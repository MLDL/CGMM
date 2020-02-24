import io
import os
import json
import requests
import zipfile
from pathlib import Path
import networkx as nx
from networkx import normalized_laplacian_matrix

import numpy as np
import torch
from torch.nn import functional as F
#import k_gnn

from sklearn.model_selection import train_test_split, StratifiedKFold
from torch_geometric.datasets import PPI

from utils.utils import NumpyEncoder
from .data import Data
from .dataloader import DataLoader
from .dataset import GraphDataset, GraphDatasetSubset
from .sampler import RandomSampler
from .tu_utils import parse_tu_data, create_graph_from_tu_data


class GraphDatasetManager:
    def __init__(self, kfold_class=StratifiedKFold, outer_k=10, inner_k=None, seed=42, holdout_test_size=0.1,
                 use_node_degree=False, use_node_attrs=False, use_one=False, DATA_DIR='DATA'):

        self.root_dir = Path(DATA_DIR) / self.name
        self.kfold_class = kfold_class
        self.holdout_test_size = holdout_test_size
        self.use_node_degree = use_node_degree
        self.use_node_attrs = use_node_attrs
        self.use_one = use_one

        if outer_k is None or outer_k == 'None':
            self.outer_k = None
        else:
            self.outer_k = int(outer_k)
        if inner_k is None or inner_k == 'None':    
            self.inner_k = None
        else:
            self.inner_k = int(inner_k)

        self.seed = seed

        self.raw_dir = self.root_dir / "raw"
        if not self.raw_dir.exists():
            os.makedirs(self.raw_dir)
            self._download()

        self.processed_dir = self.root_dir / "processed"
        if not self.processed_dir.exists():
            os.makedirs(self.processed_dir)
            self._process()

        if not (self.processed_dir / f"{self.name}.pt").exists():
            self._process()

        self.dataset = GraphDataset(torch.load(
            self.processed_dir / f"{self.name}.pt"))

        splits_filename = self.processed_dir / f"{self.name}_splits.json"
        if not splits_filename.exists():
            self.splits = []
            self._make_splits()
        else:
            self.splits = json.load(open(splits_filename, "r"))

    @property
    def num_graphs(self):
        return len(self.dataset)

    @property
    def dim_target(self):
        if not hasattr(self, "_dim_target") or self._dim_target is None:
            # not very efficient, but it works
            # todo not general enough, we may just remove it
            self._dim_target = np.unique(self.dataset.get_targets()).size
        return self._dim_target

    @property
    def dim_features(self):
        if not hasattr(self, "_dim_features") or self._dim_features is None:
            # not very elegant, but it works
            # todo not general enough, we may just remove it
            self._dim_features = self.dataset.data[0].x.size(1)
        return self._dim_features

    def _process(self):
        raise NotImplementedError

    def _download(self):
        raise NotImplementedError

    def _make_splits(self):
        """
        DISCLAIMER: train_test_split returns a SUBSET of the input indexes,
            whereas StratifiedKFold.split returns the indexes of the k subsets, starting from 0 to ...!
        """

        targets = self.dataset.get_targets()
        all_idxs = np.arange(len(targets))

        if self.outer_k is None:  # holdout assessment strategy
            assert self.holdout_test_size is not None

            if self.holdout_test_size == 0:
                train_o_split, test_split = all_idxs, []
            else:
                outer_split = train_test_split(all_idxs,
                                               stratify=targets,
                                               test_size=self.holdout_test_size)
                train_o_split, test_split = outer_split
            split = {"test": all_idxs[test_split], 'model_selection': []}

            train_o_targets = targets[train_o_split]

            if self.inner_k is None:  # holdout model selection strategy
                if self.holdout_test_size == 0:
                    train_i_split, val_i_split = train_o_split, []
                else:
                    train_i_split, val_i_split = train_test_split(train_o_split,
                                                                  stratify=train_o_targets,
                                                                  test_size=self.holdout_test_size)
                split['model_selection'].append(
                    {"train": train_i_split, "validation": val_i_split})

            else:  # cross validation model selection strategy
                inner_kfold = self.kfold_class(
                    n_splits=self.inner_k, shuffle=True)
                for train_ik_split, val_ik_split in inner_kfold.split(train_o_split, train_o_targets):
                    split['model_selection'].append(
                        {"train": train_o_split[train_ik_split], "validation": train_o_split[val_ik_split]})

            self.splits.append(split)

        else:  # cross validation assessment strategy

            outer_kfold = self.kfold_class(
                n_splits=self.outer_k, shuffle=True)

            for train_ok_split, test_ok_split in outer_kfold.split(X=all_idxs, y=targets):
                split = {"test": all_idxs[test_ok_split], 'model_selection': []}

                train_ok_targets = targets[train_ok_split]

                if self.inner_k is None:  # holdout model selection strategy
                    assert self.holdout_test_size is not None
                    train_i_split, val_i_split = train_test_split(train_ok_split,
                                                                  stratify=train_ok_targets,
                                                                  test_size=self.holdout_test_size)
                    split['model_selection'].append(
                        {"train": train_i_split, "validation": val_i_split})

                else:  # cross validation model selection strategy
                    inner_kfold = self.kfold_class(
                        n_splits=self.inner_k, shuffle=True)
                    for train_ik_split, val_ik_split in inner_kfold.split(train_ok_split, train_ok_targets):
                        split['model_selection'].append(
                            {"train": train_ok_split[train_ik_split], "validation": train_ok_split[val_ik_split]})

                self.splits.append(split)

        filename = self.processed_dir / f"{self.name}_splits.json"
        with open(filename, "w") as f:
            json.dump(self.splits[:], f, cls=NumpyEncoder)

    def _get_loader(self, dataset, batch_size=1, shuffle=True):
        # dataset = GraphDataset(data)
        sampler = RandomSampler(dataset) if shuffle is True else None

        # 'shuffle' needs to be set to False when instantiating the DataLoader,
        # because pytorch  does not allow to use a custom sampler with shuffle=True.
        # Since our shuffler is a random shuffler, either one wants to do shuffling
        # (in which case he should instantiate the sampler and set shuffle=False in the
        # DataLoader) or he does not (in which case he should set sampler=None
        # and shuffle=False when instantiating the DataLoader)

        return DataLoader(dataset,
                          batch_size=batch_size,
                          sampler=sampler,
                          shuffle=False,  # if shuffle is not None, must stay false, ow is shuffle is false
                          pin_memory=True)

    def get_test_fold(self, outer_idx, batch_size=1, shuffle=True):
        outer_idx = outer_idx or 0

        idxs = self.splits[outer_idx]["test"]
        test_data = GraphDatasetSubset(self.dataset.get_data(), idxs)

        if len(test_data) == 0:
            test_loader = None
        else:
            test_loader = self._get_loader(test_data, batch_size, shuffle)

        return test_loader

    def get_model_selection_fold(self, outer_idx, inner_idx=None, batch_size=1, shuffle=True):
        outer_idx = outer_idx or 0
        inner_idx = inner_idx or 0

        idxs = self.splits[outer_idx]["model_selection"][inner_idx]
        train_data = GraphDatasetSubset(self.dataset.get_data(), idxs["train"])
        val_data = GraphDatasetSubset(self.dataset.get_data(), idxs["validation"])

        train_loader = self._get_loader(train_data, batch_size, shuffle)

        if len(val_data) == 0:
            val_loader = None
        else:
            val_loader = self._get_loader(val_data, batch_size, shuffle)

        return train_loader, val_loader


class TUDatasetManager(GraphDatasetManager):
    URL = "https://ls11-www.cs.tu-dortmund.de/people/morris/graphkerneldatasets/{name}.zip"
    classification = True

    def _download(self):
        url = self.URL.format(name=self.name)
        response = requests.get(url)
        stream = io.BytesIO(response.content)
        with zipfile.ZipFile(stream) as z:
            for fname in z.namelist():
                z.extract(fname, self.raw_dir)

    def _process(self):
        graphs_data, num_node_labels, num_edge_labels = parse_tu_data(self.name, self.raw_dir)
        targets = graphs_data.pop("graph_labels")

        # dynamically set maximum num nodes (useful if using dense batching, e.g. diffpool)
        max_num_nodes = max([len(v) for (k, v) in graphs_data['graph_nodes'].items()])
        setattr(self, 'max_num_nodes', max_num_nodes)

        dataset = []
        for i, target in enumerate(targets, 1):
            graph_data = {k: v[i] for (k, v) in graphs_data.items()}
            G = create_graph_from_tu_data(graph_data, target, num_node_labels, num_edge_labels)

            if G.number_of_nodes() > 1 and G.number_of_edges() > 0:
                data = self._to_data(G)
                dataset.append(data)

        torch.save(dataset, self.processed_dir / f"{self.name}.pt")

    def _to_data(self, G):
        datadict = {}

        node_features = G.get_x(self.use_node_attrs, self.use_node_degree, self.use_one)
        datadict.update(x=node_features)

        if G.laplacians is not None:
            datadict.update(laplacians=G.laplacians)
            datadict.update(v_plus=G.v_plus)

        edge_index = G.get_edge_index()
        datadict.update(edge_index=edge_index)

        if G.has_edge_attrs:
            edge_attr = G.get_edge_attr()
            datadict.update(edge_attr=edge_attr)

        target = G.get_target(classification=self.classification)
        datadict.update(y=target)

        data = Data(**datadict)

        return data

    def _precompute_assignments(self):
        pass


class NCI1(TUDatasetManager):
    name = "NCI1"
    _dim_features = 37
    _dim_target = 2
    max_num_nodes = 111


class Proteins(TUDatasetManager):
    name = "PROTEINS_full"
    _dim_features = 3
    _dim_target = 2
    max_num_nodes = 620


class DD(TUDatasetManager):
    name = "DD"
    _dim_features = 89
    _dim_target = 2
    max_num_nodes = 5748


class IMDBBinary(TUDatasetManager):
    name = "IMDB-BINARY"
    _dim_features = 1
    _dim_target = 2
    max_num_nodes = 136


class IMDBMulti(TUDatasetManager):
    name = "IMDB-MULTI"
    _dim_features = 1
    _dim_target = 3
    max_num_nodes = 89


class Collab(TUDatasetManager):
    name = "COLLAB"
    _dim_features = 1
    _dim_target = 3
    max_num_nodes = 492


class Ppi(TUDatasetManager):
    name = "PPI"
    _dim_features = 49
    _dim_target = 121
    max_num_nodes = -1

    def _download(self):
        # Downloads and stores in raw_dir
        PPI(root=self.raw_dir, split='train')
        PPI(root=self.raw_dir, split='val')
        PPI(root=self.raw_dir, split='test')

    def _make_splits(self):
        split = {"test": [22, 23], 'model_selection': []}
        split['model_selection'].append({"train": [i for i in range(20)], "validation": [20, 21]})
        self.splits.append(split)

        filename = self.processed_dir / f"{self.name}_splits.json"
        with open(filename, "w") as f:
            json.dump(self.splits[:], f, cls=NumpyEncoder)

    def _process(self):

        # Reload train, val and test .pt files
        self.train = PPI(root=self.raw_dir, split='train')
        self.validation = PPI(root=self.raw_dir, split='val')
        self.test = PPI(root=self.raw_dir, split='test')

        # dynamically set maximum num nodes (useful if using dense batching, e.g. diffpool)
        max_num_nodes = max([g.x.shape[0] for data in [self.train, self.validation, self.test] for g in data])
        setattr(self, 'max_num_nodes', max_num_nodes)

        # 11-th feature is a constant, let's remove it
        idx_to_remove = 10
        mask = torch.LongTensor(list(range(idx_to_remove)) + list(range(idx_to_remove+1, 50)) )

        # Convert PyG Data object into our augmented Data object
        dataset = [Data(x=g.x.index_select(1, mask), y=g.y, edge_index=g.edge_index) for data in [self.train, self.validation, self.test] for g in data]

        '''
        Used to debug feature filtering
        for g in dataset:
            print(g.x.shape)
            print(g.x.index_select(1, mask).shape)
        '''

        torch.save(dataset, self.processed_dir / f"{self.name}.pt")

    def _to_data(self, G):
        pass



