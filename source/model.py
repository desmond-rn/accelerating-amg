import torch
import torch.nn as nn
import torch.nn.functional as F
import dgl
import dgl.nn as dglnn
from dgl.data import DGLDataset
from dgl.data.utils import save_graphs, load_graphs
import matlab
import numpy as np
from scipy.sparse import csr_matrix

from data import As_poisson_grid
# from graph_net_model import EncodeProcessDecodeNonRecurrent


def get_model(model_name, model_config, run_config, matlab_engine, train=False, train_config=None):
    dummy_input = As_poisson_grid(1, 7 ** 2)[0]
    checkpoint_dir = './training_dir/' + model_name
    graph_model, optimizer, global_step = load_model(checkpoint_dir, dummy_input, model_config,
                                                     run_config,
                                                     matlab_engine, get_optimizer=train,
                                                     train_config=train_config)
    if train:
        return graph_model, optimizer, global_step
    else:
        return graph_model


def load_model(checkpoint_dir, dummy_input, model_config, run_config, matlab_engine, get_optimizer=True,
               train_config=None):
    tf.compat.v1.enable_eager_execution()
    model = create_model(model_config)

    # we have to use the model at least once to get the list of variables
    model(csrs_to_graphs_tuple([dummy_input], matlab_engine, coarse_nodes_list=np.array([[0, 1]]),
                               baseline_P_list=[tf.convert_to_tensor(dummy_input.toarray()[:, [0, 1]])],
                               node_indicators=run_config.node_indicators,
                               edge_indicators=run_config.edge_indicators))

    variables = model.get_all_variables()
    variables_dict = {variable.name: variable for variable in variables}
    if get_optimizer:
        global_step = tf.train.get_or_create_global_step()
        decay_steps = 100
        decay_rate = 1.0
        learning_rate = tf.train.exponential_decay(train_config.learning_rate, global_step, decay_steps, decay_rate)
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)

        checkpoint = tf.train.Checkpoint(**variables_dict, optimizer=optimizer, global_step=global_step)
    else:
        optimizer = None
        global_step = None
        checkpoint = tf.train.Checkpoint(**variables_dict)
    latest_checkpoint = tf.train.latest_checkpoint(checkpoint_dir)
    if latest_checkpoint is None:
        raise RuntimeError(f'training_dir {checkpoint_dir} does not exist')
    checkpoint.restore(latest_checkpoint)
    return model, optimizer, global_step


def create_model(model_config):
    with tf.device('/gpu:0'):
        return EncodeProcessDecodeNonRecurrent(num_cores=model_config.mp_rounds, edge_output_size=1,
                                               node_output_size=1, global_block=model_config.global_block,
                                               latent_size=model_config.latent_size,
                                               num_layers=model_config.mlp_layers,
                                               concat_encoder=model_config.concat_encoder)


class AMGDataset(DGLDataset):
    """
    A class to convert an inhouse dataset (set of matrices) into a DGLdataset
    """
    def __init__(self, data, data_config):
        self.data = data
        self.dtype = data_config.dtype
        save_path = f"../data/periodic_delaunay_num_As_{len(data.As)}_num_points_{data_config.num_unknowns}" \
            f"_rnb_{data_config.root_num_blocks}"
        super(AMGDataset, self).__init__(name='AMG', save_dir=save_path)

    def process(self):
        As = self.data.As
        # Ss = self.data.Ss
        coarse_nodes_list = self.data.coarse_nodes_list
        # baseline_Ps = self.data.baseline_P_list
        sparsity_patterns = self.data.sparsity_patterns

        self.num_graphs = len(As)
        self.graphs = []
        dtype = torch.float64
        if self.dtype=='single':
            dtype = torch.float32

        for i in range(self.num_graphs):
            ## Add edges features
            g = dgl.from_scipy(As[i], eweight_name='A')
            rows, cols = sparsity_patterns[i]
            A_coo = As[i].tocoo()

            # construct numpy structured arrays, where each element is a tuple (row,col), so that we can later use
            # the numpy set function in1d()
            baseline_P_indices = np.core.records.fromarrays([rows, cols], dtype='i,i')
            coo_indices = np.core.records.fromarrays([A_coo.row, A_coo.col], dtype='i,i')

            same_indices = np.in1d(coo_indices, baseline_P_indices, assume_unique=True)
            baseline_edges = same_indices.astype(np.float64)
            non_baseline_edges = (~same_indices).astype(np.float64)

            g = dgl.graph((A_coo.row, A_coo.col))

            g.edata['A'] = torch.as_tensor(A_coo.data, dtype=dtype)
            g.edata['SP1'] = torch.as_tensor(baseline_edges, dtype=dtype)
            g.edata['SP0'] = torch.as_tensor(non_baseline_edges, dtype=dtype)

            ## Add node features
            coarse_indices = np.in1d(range(As[i].shape[0]), coarse_nodes_list[i], assume_unique=True)
            coarse_node_encodings = coarse_indices.astype(np.float64)
            fine_node_encodings = (~coarse_indices).astype(np.float64)

            g.ndata['C'] = torch.as_tensor(coarse_node_encodings, dtype=dtype)
            g.ndata['F'] = torch.as_tensor(fine_node_encodings, dtype=dtype)

            self.graphs.append(g)

        ## Delete data used for creation
        self.__dict__.pop('data', None)

    def __getitem__(self, i):
        return self.graphs[i]

    def save(self):
        save_graphs(self.save_path, self.graphs)

    def load(self):
        self.graphs, _ = load_graphs(self.save_path)

    def __len__(self):
        return self.num_graphs

class AMGModel(nn.Module):
    def __init__(self, model_config):
        super().__init__()
        h_feats = model_config.latent_size

        ## Encode nodes
        self.W1, self.W2 = self.create_MLP(2, h_feats, h_feats)

        ## Encode edges
        self.W5, self.W6 = self.create_MLP(3, h_feats, h_feats)

        ## Process
        self.conv1 = dglnn.SAGEConv(
                    in_feats=h_feats, out_feats=h_feats, aggregator_type='mean')
        self.conv2 = dglnn.SAGEConv(
                    in_feats=2*h_feats, out_feats=h_feats, aggregator_type='mean')
        self.conv3 = dglnn.SAGEConv(
                    in_feats=2*h_feats, out_feats=h_feats, aggregator_type='mean')

        ## Decode edges
        self.W9, self.W10 = self.create_MLP(2*h_feats, h_feats, 1)    ## Concat source and dest before doing this

    def create_MLP(self, in_feats, hidden_feats, out_feats):
        W1 = nn.Linear(in_feats, hidden_feats)
        W2 = nn.Linear(hidden_feats, out_feats)
        return W1, W2

    def encode_nodes(self, nodes):
        h = torch.cat([nodes['C'], nodes['F']], 1)
        return {'node_encs': self.W2(F.relu(self.W1(h)))}

    def encode_edges(self, edges):
        h = torch.cat([edges.data['A'], edges.data['SP1'], edges.data['SP0']], 1)
        return {'edge_encs': self.W6(F.relu(self.W5(h)))}

    def decode_edges(self, edges):
        h = torch.cat([edges.src['h'], edges.dst['h']], 1)          ##Key here
        return {'new_P': self.W10(F.relu(self.W9(h))).squeeze(1)}

    def forward(self, g, h):
        with g.local_scope():

            ## Encode nodes
            g.apply_nodes(self.encode_nodes)
            
            ## Encode edges
            g.apply_edges(self.encode_edges)

            ## Message passing
            n_encs = g.ndata['node_encs']
            e_encs = g.edata['edge_encs']
            # e_encs = g.edata['A']

            h = self.conv1(g, n_encs, edge_weight=e_encs)
            h = F.relu(h)

            h = torch.cat([h, n_encs], 1)
            h = self.conv2(g, h, edge_weight=e_encs)
            h = F.relu(h)
            
            h = torch.cat([h, n_encs], 1)
            h = self.conv2(g, h, edge_weight=e_encs)

            ## Decode edges
            g.ndata['h'] = h
            g.apply_edges(self.decode_edges)

            new_P = g.edata['new_P']
            # return g.edata['new_P']
            # return g

        ### <<------- Trick to have local scope and keep newP ---------->>
        if 'new_P' in g.edata:
            return g
        else:
            g.edata['new_P'] = new_P
            return g

def csrs_to_graphs_tuple(csrs, matlab_engine, node_feature_size=128, coarse_nodes_list=None, baseline_P_list=None,
                         node_indicators=True, edge_indicators=True):
    dtype = tf.float64

    # build up the arguments for the GraphsTuple constructor
    n_node = tf.convert_to_tensor([csr.shape[0] for csr in csrs])
    n_edge = tf.convert_to_tensor([csr.nnz for csr in csrs])

    if not edge_indicators:
        numpy_edges = np.concatenate([csr.data for csr in csrs])
        edges = tf.expand_dims(tf.convert_to_tensor(numpy_edges, dtype=dtype), axis=1)
    else:
        edge_encodings_list = []
        for csr, coarse_nodes, baseline_P in zip(csrs, coarse_nodes_list, baseline_P_list):
            if tf.is_tensor(baseline_P):
                baseline_P = csr_matrix(baseline_P.numpy())

            baseline_P_rows, baseline_P_cols = P_square_sparsity_pattern(baseline_P, baseline_P.shape[0],
                                                                         coarse_nodes, matlab_engine)
            coo = csr.tocoo()

            # construct numpy structured arrays, where each element is a tuple (row,col), so that we can later use
            # the numpy set function in1d()
            baseline_P_indices = np.core.records.fromarrays([baseline_P_rows, baseline_P_cols], dtype='i,i')
            coo_indices = np.core.records.fromarrays([coo.row, coo.col], dtype='i,i')

            same_indices = np.in1d(coo_indices, baseline_P_indices, assume_unique=True)
            baseline_edges = same_indices.astype(np.float64)
            non_baseline_edges = (~same_indices).astype(np.float64)

            edge_encodings = np.stack([coo.data, baseline_edges, non_baseline_edges]).T
            edge_encodings_list.append(edge_encodings)
        numpy_edges = np.concatenate(edge_encodings_list)
        edges = tf.convert_to_tensor(numpy_edges, dtype=dtype)

    # COO format for sparse matrices contains a list of row indices and a list of column indices
    coos = [csr.tocoo() for csr in csrs]
    senders_numpy = np.concatenate([coo.row for coo in coos])
    senders = tf.convert_to_tensor(senders_numpy)
    receivers_numpy = np.concatenate([coo.col for coo in coos])
    receivers = tf.convert_to_tensor(receivers_numpy)

    # see the source of _concatenate_data_dicts for explanation
    offsets = gn.utils_tf._compute_stacked_offsets(n_node, n_edge)
    senders += offsets
    receivers += offsets

    if not node_indicators:
        nodes = None
    else:
        node_encodings_list = []
        for csr, coarse_nodes in zip(csrs, coarse_nodes_list):
            coarse_indices = np.in1d(range(csr.shape[0]), coarse_nodes, assume_unique=True)

            coarse_node_encodings = coarse_indices.astype(np.float64)
            fine_node_encodings = (~coarse_indices).astype(np.float64)
            node_encodings = np.stack([coarse_node_encodings, fine_node_encodings]).T

            node_encodings_list.append(node_encodings)

        numpy_nodes = np.concatenate(node_encodings_list)
        nodes = tf.convert_to_tensor(numpy_nodes, dtype=dtype)

    graphs_tuple = gn.graphs.GraphsTuple(
        nodes=nodes,
        edges=edges,
        globals=None,
        receivers=receivers,
        senders=senders,
        n_node=n_node,
        n_edge=n_edge
    )
    if not node_indicators:
        graphs_tuple = gn.utils_tf.set_zero_node_features(graphs_tuple, 1, dtype=dtype)

    graphs_tuple = gn.utils_tf.set_zero_global_features(graphs_tuple, node_feature_size, dtype=dtype)

    return graphs_tuple



def to_prolongation_matrix_csr(matrix, coarse_nodes, baseline_P, nodes, normalize_rows=True,
                               normalize_rows_by_node=False):
    """
    sparse version of the above function, for when the dense matrix is too large to fit in GPU memory
    used only for inference, so no need for backpropagation, inputs are csr matrices
    """
    # prolongation from coarse point to itself should be identity. This corresponds to 1's on the diagonal
    matrix.setdiag(np.ones(matrix.shape[0]))

    # select only columns corresponding to coarse nodes
    matrix = matrix[:, coarse_nodes]

    # set sparsity pattern (interpolatory sets) to be of baseline prolongation
    baseline_P_mask = (baseline_P != 0).astype(np.float64)
    matrix = matrix.multiply(baseline_P_mask)
    matrix.eliminate_zeros()

    if normalize_rows:
        if normalize_rows_by_node:
            baseline_row_sum = nodes
        else:
            baseline_row_sum = baseline_P.sum(axis=1)
            baseline_row_sum = np.array(baseline_row_sum)[:, 0]

        matrix_row_sum = np.array(matrix.sum(axis=1))[:, 0]
        # https://stackoverflow.com/a/12238133
        matrix_copy = matrix.copy()
        matrix_copy.data /= matrix_row_sum.repeat(np.diff(matrix_copy.indptr))
        matrix_copy.data *= baseline_row_sum.repeat(np.diff(matrix_copy.indptr))
        matrix = matrix_copy
    return matrix


def to_prolongation_matrix_tensor(matrix, coarse_nodes, baseline_P, nodes,
                                  normalize_rows=True,
                                  normalize_rows_by_node=False):
    dtype = torch.float64
    matrix = matrix.to_dense().type(dtype)

    # prolongation from coarse point to itself should be identity. This corresponds to 1's on the diagonal
    num_rows = matrix.shape[0]
    new_diag = torch.ones(num_rows, dtype=dtype)
    matrix[range(num_rows), range(num_rows)] = new_diag
    
    # select only columns corresponding to coarse nodes
    matrix = matrix[:, coarse_nodes]

    # set sparsity pattern (interpolatory sets) to be of baseline prolongation
    baseline_P = torch.as_tensor(baseline_P, dtype=dtype)
    baseline_zero_mask = torch.as_tensor(torch.not_equal(baseline_P, torch.zeros_like(baseline_P)), dtype=dtype)
    matrix = matrix * baseline_zero_mask

    if normalize_rows:
        if normalize_rows_by_node:
            baseline_row_sum = nodes
        else:
            baseline_row_sum = torch.sum(baseline_P, dim=1, dtype=dtype)

        matrix_row_sum = torch.sum(matrix, dim=1, dtype=dtype)

        # there might be a few rows that are all 0's - corresponding to fine points that are not connected to any
        # coarse point. We use "nan_to_num" to put these rows to 0's
        matrix = torch.divide(matrix, torch.reshape(matrix_row_sum, (-1, 1)))
        matrix = torch.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)

        matrix = matrix * torch.reshape(baseline_row_sum, (-1, 1))

    return matrix[:, coarse_nodes], matrix



def dgl_graph_to_sparse_matrices(dgl_graph, val_feature='P', return_nodes=False):
    num_graphs = len(dgl_graph)
    graphs = [dgl_graph[i] for i in range(num_graphs)]

    matrices = []
    nodes_lists = []
    for graph in graphs:
        indices = torch.stack(graph.edges(), axis=0)
        indices = graph.edata[val_feature]
        n_nodes = graph.num_nodes()
        matrix = torch.sparse_coo_tensor(indices, indices, (n_nodes, n_nodes))
         # reordering is required because the pyAMG coarsening step does not preserve indices order
        matrix = matrix.coalesce()
        matrices.append(matrix)

    if return_nodes:
        for graph in graphs:
            nodes_list = graph.nodes()
            nodes_lists.append(nodes_list)
        return matrices, nodes_lists
    else: 
        return matrices

def graphs_tuple_to_sparse_matrices(graphs_tuple, return_nodes=False):
    num_graphs = int(graphs_tuple.n_node.shape[0])
    graphs = [gn.utils_tf.get_graph(graphs_tuple, i)
              for i in range(num_graphs)]

    matrices = [graphs_tuple_to_sparse_tensor(graph) for graph in graphs]

    if return_nodes:
        nodes_list = [tf.squeeze(graph.nodes) for graph in graphs]
        return matrices, nodes_list
    else:
        return matrices


def graphs_tuple_to_sparse_tensor(graphs_tuple):
    senders = graphs_tuple.senders
    receivers = graphs_tuple.receivers
    indices = tf.cast(tf.stack([senders, receivers], axis=1), tf.int64)

    # first element in the edge feature is the value, the other elements are metadata
    values = tf.squeeze(graphs_tuple.edges[:, 0])

    shape = tf.concat([graphs_tuple.n_node, graphs_tuple.n_node], axis=0)
    shape = tf.cast(shape, tf.int64)

    matrix = tf.sparse.SparseTensor(indices, values, shape)
    # reordering is required because the pyAMG coarsening step does not preserve indices order
    matrix = tf.sparse.reorder(matrix)

    return matrix
