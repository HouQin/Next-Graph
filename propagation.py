import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
from scipy.linalg import expm

from utils import MixedDropout, sparse_matrix_to_torch

def full_attention_conv(qs, ks):
    '''
    qs: query tensor [N, H, M]
    ks: key tensor [L, H, M]
    vs: value tensor [L, H, D]

    return output [N, H, D]
    '''
    # normalize input
    qs = qs / torch.norm(qs, p=2) # [N, H, M]
    ks = ks / torch.norm(ks, p=2) # [L, H, M]
    N = qs.shape[0]

    # denominator
    all_ones = torch.ones([ks.shape[0]]).to(ks.device)
    ks_sum = torch.einsum("lhm,l->hm", ks, all_ones)
    attention_normalizer = torch.einsum("nhm,hm->nh", qs, ks_sum)  # [N, H]

    # attentive aggregated results
    attention_normalizer = torch.unsqueeze(attention_normalizer, len(attention_normalizer.shape))  # [N, H, 1]
    attention_normalizer += torch.ones_like(attention_normalizer) * N

    # compute attention for visualization if needed
    attention = torch.einsum("nhm,lhm->nhl", qs, ks) / attention_normalizer # [N, L, H]
    return attention

def calc_A_hat(adj_matrix: sp.spmatrix) -> sp.spmatrix:
    nnodes = adj_matrix.shape[0]
    A = adj_matrix + sp.eye(nnodes)
    D_vec = np.sum(A, axis=1).A1
    D_vec_invsqrt_corr = 1 / np.sqrt(D_vec)
    # D_vec_invsqrt_corr = 1 / D_vec
    D_invsqrt_corr = sp.diags(D_vec_invsqrt_corr)
    return D_invsqrt_corr @ A @ D_invsqrt_corr
    # return D_invsqrt_corr @ A

def calc_ppr_exact(adj_matrix: sp.spmatrix, alpha: float) -> np.ndarray:
    nnodes = adj_matrix.shape[0]
    M = calc_A_hat(adj_matrix)
    A_inner = sp.eye(nnodes) - (1 - alpha) * M
    return alpha * np.linalg.inv(A_inner.toarray())

class PPRExact(nn.Module):
    def __init__(self, adj_matrix: sp.spmatrix, alpha: float, drop_prob: float = None):
        super().__init__()

        ppr_mat = calc_ppr_exact(adj_matrix, alpha)
        self.register_buffer('mat', torch.FloatTensor(ppr_mat))

        if drop_prob is None or drop_prob == 0:
            self.dropout = lambda x: x
        else:
            self.dropout = MixedDropout(drop_prob)

    def forward(self, predictions: torch.FloatTensor, idx: torch.LongTensor):
        return self.dropout(self.mat[idx]) @ predictions

class PPRPowerIteration(nn.Module):
    def __init__(self, adj_matrix: sp.spmatrix, num_heads: int, nclasses: int, niter: int, npow: int, npow_attn: int, nalpha:float, num_feature: int = None, num_hidden: int = None, drop_prob: float = None, device=None, niter_attn: int = None):
        '''
        Parameters
        ----------
        adj_matrix : 原始的图
        niter : 阶数
        npow : 跳
        nalpha : APGNN前面的系数
        drop_prob : dropout
        '''
        super().__init__()

        self.niter = niter
        if niter_attn is None:
            self.niter_attn = niter
        else:
            self.niter_attn = niter_attn
        self.npow = npow
        self.npow_attn = npow_attn
        self.nalpha=nalpha
        self.device = device
        self.num_view = 1
        self.num_heads = num_heads
        self.nclasses = nclasses
        if num_hidden is not None:
            self.num_hidden = num_hidden
        if num_feature is not None:
            self.num_feature = num_feature

        M = calc_A_hat(adj_matrix)
        tempM = M
        for ti in range(self.npow):
            M = M @ tempM
        M = nalpha*M
        self.A = M.todense()
        self.A = torch.from_numpy(self.A).float().to(device)
        self.A = [self.A]
        self.A = torch.stack(self.A, dim=0)
        self.fc1 = nn.Sequential(
            nn.Linear(niter, 1),
        )
        if num_hidden is None:
            self.Wk = nn.Linear(nclasses, nclasses * num_heads)
            self.Wq = nn.Linear(nclasses, nclasses * num_heads)
        else:
            self.Wk = nn.Linear(num_feature, num_hidden * num_heads)
            self.Wq = nn.Linear(num_feature, num_hidden * num_heads)

        self.linear1 = nn.Linear(niter*self.num_view, 1)
        self.linear2 = nn.Linear(niter_attn*self.num_view, 1)
        self.softmax = torch.nn.Softmax(dim=0)

        if drop_prob is None or drop_prob == 0:
            self.dropout = lambda x: x
        else:
            self.dropout = MixedDropout(drop_prob)

        def reset_parameters(self):
            self.Wk.reset_parameters()
            self.Wq.reset_parameters()

    def forward(self, local_preds: torch.FloatTensor, idx: torch.LongTensor, origin_fea: torch.sparse.FloatTensor=None):
        preds = local_preds.float()
        if origin_fea is None:
            source_preds = preds.clone()
            query = self.Wq(preds).reshape(-1, self.num_heads, self.nclasses)
            key = self.Wk(source_preds).reshape(-1, self.num_heads, self.nclasses)
            Attn = full_attention_conv(query, key)
            Attn = Attn.mean(dim=1).unsqueeze(0)
        else:
            source_preds = origin_fea.clone()
            query = self.Wq(origin_fea).reshape(-1, self.num_heads, self.num_hidden)
            key = self.Wk(source_preds).reshape(-1, self.num_heads, self.num_hidden)
            Attn = full_attention_conv(query, key)
            Attn = Attn.mean(dim=1).unsqueeze(0)

        temp_Attn = Attn
        for ti in range(self.npow_attn):
            Attn = Attn @ temp_Attn

        all_one = torch.ones(self.num_view).to(self.device)

        M__ = None
        tmp = None
        # for i in range(0, self.niter):
        #     if i == 0:
        #         M__ = preds.unsqueeze(0)
        #         M__ = torch.einsum('v, lnd->vnd', all_one, M__)
        #
        #         tmp = preds
        #         tmp = tmp.unsqueeze(0)
        #
        #     else:
        #         tmp = torch.einsum('v, lnd->vnd', all_one, tmp)
        #         tmp = torch.bmm(Attn, tmp)
        #         M__ = torch.cat([M__, tmp], dim=0)
        #
        # beta = self.linear2.weight.t().unsqueeze(1)
        # preds = torch.sum(beta * M__, dim=0)
        M__ = torch.eye(Attn.shape[-1]).to(self.device)
        M__ = M__.unsqueeze(0)
        beta = self.linear2.weight.t()
        for i in range(0, self.niter_attn):
            M__ = M__ + beta[i, 0] * torch.pow(Attn, i+1)

        tmp = preds.unsqueeze(0)
        tmp = torch.einsum('v, lnd->vnd', all_one, tmp)

        preds = torch.bmm(M__, tmp)
        preds = torch.sum(preds, dim=0)

        M__ = None
        tmp = None
        for i in range(0, self.niter):
            if i == 0:
                M__ = preds.unsqueeze(0)
                M__ = torch.einsum('v, lnd->vnd', all_one, M__)

                tmp = preds
                tmp = tmp.unsqueeze(0)

            else:
                tmp = torch.einsum('v, lnd->vnd', all_one, tmp)
                tmp = torch.bmm(self.A, tmp)
                M__ = torch.cat([M__, tmp], dim=0)

        alph = self.linear1.weight.t().unsqueeze(1)
        preds = torch.sum(alph * M__, dim=0)

        return preds[idx]
