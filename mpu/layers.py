# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
# Parts of the code here are adapted from PyTorch
# repo: https://github.com/pytorch/pytorch
# update by cxy,2020-0408

import math

import torch
import torch.nn.functional as F
import torch.nn.init as init
from torch.nn.parameter import Parameter

# from apex.normalization.fused_layer_norm import FusedLayerNorm as LayerNorm

from .initialize import get_model_parallel_rank,get_data_parallel_rank
from .initialize import get_model_parallel_world_size,get_data_parallel_world_size
from .mappings import copy_to_model_parallel_region
from .mappings import gather_from_model_parallel_region
from .mappings import gather_from_model_parallel_region_align_target_dim
from .mappings import reduce_from_model_parallel_region
from .mappings import scatter_to_model_parallel_region
from .random import get_cuda_rng_tracker
from .utils import divide  #,divide_ceil
from .utils import split_tensor_along_last_dim
from .utils import VocabUtility


def _initialize_affine_weight(weight, output_size, input_size,
                              per_partition_size, partition_dim, init_method,
                              stride=1, return_master_weight=False):
    """Initialize affine weight for model parallel.

    Build the master weight on all processes and scatter
    the relevant chunk."""
    # If we only use 1 process for model parallelism, bypass scatter.
    world_size = get_model_parallel_world_size()
    if world_size == 1:
        init_method(weight)
        if return_master_weight:
            return weight
        return None

    # Initialize master weight
    master_weight = torch.empty(output_size, input_size,
                                dtype=weight.dtype,
                                requires_grad=False)
    init_method(master_weight)

    # Split and copy
    per_partition_per_stride_size = divide(per_partition_size, stride)
    weight_list = torch.split(master_weight, per_partition_per_stride_size,
                              dim=partition_dim)
    rank = get_model_parallel_rank()
    my_weight_list = weight_list[rank::world_size]

    with torch.no_grad():
        torch.cat(my_weight_list, dim=partition_dim, out=weight)
    if return_master_weight:
        return master_weight
    return None


class VocabParallelEmbedding(torch.nn.Module):
    """Embedding parallelized in the vocabulary dimension.

    This is mainly adapted from torch.nn.Embedding and all the default
    values are kept.
    Arguments:
        num_embeddings: vocabulary size.
        embedding_dim: size of hidden state.
        init_method: method to initialize weights.
    """
    def __init__(self, num_embeddings, embedding_dim,
                 init_method=init.xavier_normal_):
        super(VocabParallelEmbedding, self).__init__()
        # Keep the input dimensions.
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        # Set the detauls for compatibility.
        self.padding_idx = None
        self.max_norm = None
        self.norm_type = 2.
        self.scale_grad_by_freq = False
        self.sparse = False
        self._weight = None
        # Divide the weight matrix along the vocaburaly dimension.
        self.vocab_start_index, self.vocab_end_index = \
            VocabUtility.vocab_range_from_global_vocab_size(
                self.num_embeddings, get_model_parallel_rank(),
                get_model_parallel_world_size())
        self.num_embeddings_per_partition = self.vocab_end_index - \
                                            self.vocab_start_index

        # Allocate weights.
        self.weight = Parameter(torch.Tensor(self.num_embeddings_per_partition,
                                             self.embedding_dim))
        self.weight.model_parallel = True
        # And initialize.
        _initialize_affine_weight(
            self.weight, self.num_embeddings, self.embedding_dim,
            self.num_embeddings_per_partition, 0, init_method)

    def forward(self, input_):
        # Build the mask.
        input_mask = (input_ < self.vocab_start_index) | \
                     (input_ >= self.vocab_end_index)
        # Mask the input.
        masked_input = input_.clone() - self.vocab_start_index
        masked_input[input_mask] = 0
        # Get the embeddings.
        output_parallel = F.embedding(masked_input, self.weight,
                                      self.padding_idx, self.max_norm,
                                      self.norm_type, self.scale_grad_by_freq,
                                      self.sparse)
        # Mask the output embedding.
        output_parallel[input_mask, :] = 0.0
        # Reduce across all the model parallel GPUs.
        output = reduce_from_model_parallel_region(output_parallel)
        return output


class ParallelEmbedding(torch.nn.Module):
    """Embedding parallelized in the embedding dimension.

    This is mainly adapted from torch.nn.Embedding and all the default
    values are kept.
    Arguments:
        num_embeddings: vocabulary size.
        embedding_dim: size of hidden state.
        init_method: method to initialize weights.
    """
    def __init__(self, num_embeddings, embedding_dim,
                 init_method=init.xavier_normal_,
                 keep_master_weight_for_test=False):
        super(ParallelEmbedding, self).__init__()
        # Keep the input dimensions.
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        # Set some detauls for compatibility.
        self.padding_idx = None
        self.max_norm = None
        self.norm_type = 2.
        self.scale_grad_by_freq = False
        self.sparse = False
        self._weight = None
        # Divide the weight matrix along the embedding dimension.
        world_size = get_model_parallel_world_size()
        self.embedding_dim_per_partition = divide(self.embedding_dim,
                                                  world_size)

        # Allocate weights.
        self.weight = Parameter(torch.Tensor(self.num_embeddings,
                                             self.embedding_dim_per_partition))
        self.weight.model_parallel = True
        # And initialize.
        _initialize_affine_weight(
            self.weight, self.num_embeddings, self.embedding_dim,
            self.embedding_dim_per_partition, 1, init_method,
            stride=1, return_master_weight=False)

    def forward(self, input_):
        input_parallel = copy_to_model_parallel_region(input_)
        output_parallel = F.embedding(input_parallel, self.weight,
                                      self.padding_idx, self.max_norm,
                                      self.norm_type, self.scale_grad_by_freq,
                                      self.sparse)
        output = gather_from_model_parallel_region(output_parallel)
        return output


class ColumnParallelLinear(torch.nn.Module):
    """Linear layer with column parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its second dimension as A = [A_1, ..., A_p].

    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias
        gather_output: If true, call all-gether on output and make Y avaiable
                       to all GPUs, otherwise, every GPU will have its output
                       which is Y_i = XA_i
        init_method: method to initialize weights. Note that bias is always set
                     to zero.
        stride: For the strided linear layers.
        keep_master_weight_for_test: This was added for testing and should be
                                     set to False. It returns the master weights
                                     used for initialization.
    """
    def __init__(self, input_size, output_size, bias=True, gather_output=True,
                 init_method=init.xavier_normal_, stride=1,
                 keep_master_weight_for_test=False):
        super(ColumnParallelLinear, self).__init__()

        # Keep input parameters
        self.input_size = input_size
        self.output_size = output_size
        self.gather_output = gather_output
        # Divide the weight matrix along the last dimension.
        world_size = get_model_parallel_world_size()
        print("ColumnParallelLinear world_size",world_size)
        self.output_size_per_partition = divide(output_size, world_size)

        # Parameters.
        # Note: torch.nn.functional.linear performs XA^T + b and as a result
        # we allocate the transpose.
        self.weight = Parameter(torch.Tensor(self.output_size_per_partition,
                                             self.input_size))
        self.weight.model_parallel = True
        if bias:
            self.bias = Parameter(torch.Tensor(self.output_size_per_partition))
            self.bias.model_parallel = True
            # Always initialize bias to zero.
            with torch.no_grad():
                self.bias.zero_()
        else:
            self.register_parameter('bias', None)

        # Initialize weight.
        self.master_weight = _initialize_affine_weight(
            self.weight, self.output_size, self.input_size,
            self.output_size_per_partition, 0, init_method,
            stride=stride, return_master_weight=keep_master_weight_for_test)

    def forward(self, input_):
        # Set up backprop all-reduce.
        input_parallel = copy_to_model_parallel_region(input_)
        # Matrix multiply.
        output_parallel = F.linear(input_parallel, self.weight, self.bias)
        if self.gather_output:
            # All-gather across the partitions.
            output = gather_from_model_parallel_region(output_parallel)
        else:
            output = output_parallel
        return output


class RowParallelLinear(torch.nn.Module):
    """Linear layer with row parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its first dimension and X along its second dimension as:
               -   -
              | A_1 |
              | .   |
          A = | .   |        X = [X_1, ..., X_p]
              | .   |
              | A_p |
               -   -
    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias. Note that bias is not parallelized.
        input_is_parallel: If true, we assume that the input is already
                           split across the GPUs and we do not split
                           again.
        init_method: method to initialize weights. Note that bias is always set
                     to zero.
        stride: For the strided linear layers.
        keep_master_weight_for_test: This was added for testing and should be
                                     set to False. It returns the master weights
                                     used for initialization.
    """
    def __init__(self, input_size, output_size, bias=True,
                 input_is_parallel=False,
                 init_method=init.xavier_normal_, stride=1,
                 keep_master_weight_for_test=False):
        super(RowParallelLinear, self).__init__()

        # Keep input parameters
        self.input_size = input_size
        self.output_size = output_size
        self.input_is_parallel = input_is_parallel
        # Divide the weight matrix along the last dimension.
        world_size = get_model_parallel_world_size()
        self.input_size_per_partition = divide(input_size, world_size)

        # Parameters.
        # Note: torch.nn.functional.linear performs XA^T + b and as a result
        # we allocate the transpose.
        self.weight = Parameter(torch.Tensor(self.output_size,
                                             self.input_size_per_partition))
        self.weight.model_parallel = True
        if bias:
            self.bias = Parameter(torch.Tensor(self.output_size))
            # Always initialize bias to zero.
            with torch.no_grad():
                self.bias.zero_()
        else:
            self.register_parameter('bias', None)

        # Initialize weight.
        self.master_weight = _initialize_affine_weight(
            self.weight, self.output_size, self.input_size,
            self.input_size_per_partition, 1, init_method,
            stride=stride, return_master_weight=keep_master_weight_for_test)

    def forward(self, input_):
        # Set up backprop all-reduce.
        if self.input_is_parallel:
            input_parallel = input_
        else:
            input_parallel = scatter_to_model_parallel_region(input_)
        # Matrix multiply.
        output_parallel = F.linear(input_parallel, self.weight)
        # All-reduce across all the partitions.
        output_ = reduce_from_model_parallel_region(output_parallel)
        if self.bias is not None:
            output = output_ + self.bias
        else:
            output = output_
        return output

class ArcfaceColumnParallelLinear(torch.nn.Module):
    """Linear layer with column parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its second dimension as A = [A_1, ..., A_p].

    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias
        gather_output: If true, call all-gether on output and make Y avaiable
                       to all GPUs, otherwise, every GPU will have its output
                       which is Y_i = XA_i
        init_method: method to initialize weights. Note that bias is always set
                     to zero.
        stride: For the strided linear layers.
        keep_master_weight_for_test: This was added for testing and should be
                                     set to False. It returns the master weights
                                     used for initialization.
    """
    def __init__(self, embedding_size, output_classs_size, bias=False, gather_output=False,
                 init_method=init.xavier_normal_, stride=1,
                 keep_master_weight_for_test=False,s=30.0, m=0.50):
        super(ArcfaceColumnParallelLinear, self).__init__()

        # Keep input parameters
        self.embedding_size = embedding_size
        self.output_classs_size = output_classs_size
        self.gather_output = gather_output
        self.scalar=s
        self.marge=m
        # self.cos_m = math.cos(m)
        # self.sin_m = math.sin(m)
        # self.mm = self.sin_m * m  # issue 1
        # self.threshold = math.cos(math.pi - m)
        ##################################
        self.register_buffer('cos_m',torch.Tensor([math.cos(m)]))
        self.register_buffer('sin_m',  torch.Tensor([math.sin(m)]))
        self.register_buffer('mm', torch.Tensor([self.sin_m * m ]))
        self.register_buffer('threshold',torch.Tensor([ math.cos(math.pi - m)]))
        # Divide the weight matrix along the last dimension.
        # world_size = get_data_parallel_world_size() #1
        world_size = get_model_parallel_world_size()  # 1
        print("ColumnParallelLinear world_size",world_size)
        self.output_size_per_partition = divide(output_classs_size, world_size)
        # self.output_size_per_partition = divide_ceil(output_classs_size, world_size)
        # Get the partition's vocab indecies
        get_vocab_range = VocabUtility.vocab_range_from_per_partition_vocab_size
        rank = get_model_parallel_rank()  # get rank in model parallel
        world_size = get_model_parallel_world_size()  # total world size
        self.vocab_start_index, self.vocab_end_index = get_vocab_range(
            self.output_size_per_partition, rank, world_size)
        # Parameters.
        # Note: torch.nn.functional.linear performs XA^T + b and as a result
        # we allocate the transpose.
        self.weight = Parameter(torch.Tensor(self.output_size_per_partition,
                                             self.embedding_size))
        self.weight.model_parallel = True
        if bias:
            self.bias = Parameter(torch.Tensor(self.output_size_per_partition))
            self.bias.model_parallel = True
            # Always initialize bias to zero.
            with torch.no_grad():
                self.bias.zero_()
        else:
            self.register_parameter('bias', None)

        # Initialize weight.
        self.master_weight = _initialize_affine_weight(
            self.weight, self.output_classs_size, self.embedding_size,
            self.output_size_per_partition, 0, init_method,
            stride=stride, return_master_weight=keep_master_weight_for_test)

#     def forward(self, input_embedding,input_labels):
#         #  gather input  embed feature from all model parallel regaion
#         input_parallel_embedding=gather_from_model_parallel_region_align_target_dim(input_embedding)# get all input X feature and Y label
#         input_parallel_labels=gather_from_model_parallel_region_align_target_dim(input_labels)
#         inputBatch = input_parallel_embedding.size()[0]
#         # cos_theta = F.linear(F.normalize(input_parallel_embedding),  F.normalize(self.weight), self.bias)
#         cos_theta = F.linear(input_parallel_embedding, F.normalize(self.weight), self.bias)
#         cos_theta = cos_theta.clamp(-1, 1)  # for numerical stability
#         cos_theta_2 = torch.pow(cos_theta, 2)
#         sin_theta_2 = 1 - cos_theta_2
#         sin_theta = torch.sqrt(sin_theta_2)
#         cos_theta_m = (cos_theta * self.cos_m - sin_theta * self.sin_m).to(dtype=cos_theta.dtype)
#         # this condition for special theta ,make cos_tehta_m math valid
#         cond_v = cos_theta - self.threshold
#         cond_mask = cond_v <= 0
#         keep_val = (cos_theta - self.mm).to(dtype=cos_theta.dtype) # when theta not in [0,pi], use cosface instead
#         cos_theta_m[cond_mask] = keep_val[cond_mask]
#         ####################################################################
#         output_parallel = cos_theta * 1.0  # a little bit hacky way to prevent in_place operation on cos_theta
#         idx = torch.arange(0, inputBatch, dtype=torch.long)
#         label_inrange_mask = (self.vocab_start_index <= input_parallel_labels) & (
#                     input_parallel_labels < self.vocab_end_index)
#         masked_valid_label = input_parallel_labels.clone() - self.vocab_start_index
#         row_idx=idx[label_inrange_mask]
#         valid_label_idx=masked_valid_label[label_inrange_mask]
#         output_parallel[row_idx, valid_label_idx] = cos_theta_m[row_idx, valid_label_idx]
#         output_parallel *= self.scalar  # scale up in order to make softmax work, first introduced in normface
#         if self.gather_output:
#             # All-gather across the partitions.
#            output = gather_from_model_parallel_region(output_parallel)
#         else:
#             output = output_parallel
#         return output,input_parallel_labels
    def forward(self, input_embedding,input_labels):
        #  gather input  embed feature from all model parallel regaion
        input_embedding = gather_from_model_parallel_region_align_target_dim(
            input_embedding)  # get all input X feature and Y label
        input_labels = gather_from_model_parallel_region_align_target_dim(input_labels)
        inputBatch = input_embedding.size()[0]

        idx = torch.arange(0, inputBatch, dtype=torch.long)
        label_inrange_mask = (self.vocab_start_index <= input_labels) & (
                input_labels < self.vocab_end_index)
        masked_valid_label = input_labels.clone() - self.vocab_start_index
        row_idx = idx[label_inrange_mask]
        valid_label_idx = masked_valid_label[label_inrange_mask]
        cos_theta = F.linear(input_embedding, F.normalize(self.weight), self.bias)

        cos_theta = cos_theta.clamp(-1, 1)  # for numerical stability

        cos_theta_valid = cos_theta[row_idx, valid_label_idx]
        cos_theta_m = (cos_theta_valid * self.cos_m - torch.sqrt(1 - torch.pow(cos_theta_valid, 2)) * self.sin_m).to(
            dtype=cos_theta.dtype)
        cos_theta_m[cos_theta_valid <= self.threshold] = (
                    cos_theta_valid[cos_theta_valid <= self.threshold] - self.mm).to(dtype=cos_theta.dtype)
        ####################################################################
        cos_theta *= 1.0  # a little bit hacky way to prevent in_place operation on cos_theta

        cos_theta[row_idx, valid_label_idx] = cos_theta_m
        cos_theta *= self.scalar  # scale up in order to make softmax work, first introduced in normface
        if self.gather_output:
            # All-gather across the partitions.
            cos_theta = gather_from_model_parallel_region(cos_theta)
        else:
            cos_theta = cos_theta
        return cos_theta, input_labels
