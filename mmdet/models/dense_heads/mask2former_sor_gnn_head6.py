# Copyright (c) OpenMMLab. All rights reserved.
import copy
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import Conv2d, build_plugin_layer, caffe2_xavier_init
from mmcv.cnn.bricks.transformer import (build_positional_encoding,
                                         build_transformer_layer_sequence)

from mmcv.ops import point_sample
from mmcv.runner import force_fp32, ModuleList

from mmdet.core import build_assigner, build_sampler, multi_apply, reduce_mean
from mmdet.models.utils import preprocess_panoptic_gt, get_uncertain_point_coords_with_randomness
from ..builder import HEADS, build_loss
from .anchor_free_head import AnchorFreeHead
from mmcv.cnn import build_norm_layer
from torch.autograd import Variable
import numpy as np


@HEADS.register_module()
class Mask2FormerGNNHead6(AnchorFreeHead):
    """Implements the Mask2Former head.

    See `Masked-attention Mask Transformer for Universal Image
    Segmentation <https://arxiv.org/pdf/2112.01527>`_ for details.

    Args:
        in_channels (list[int]): Number of channels in the input feature map.
        feat_channels (int): Number of channels for features.
        out_channels (int): Number of channels for output.
        num_things_classes (int): Number of things.
        num_stuff_classes (int): Number of stuff.
        num_queries (int): Number of query in Transformer decoder.
        pixel_decoder (:obj:`mmcv.ConfigDict` | dict): Config for pixel
            decoder. Defaults to None.
        enforce_decoder_input_project (bool, optional): Whether to add
            a layer to change the embed_dim of tranformer encoder in
            pixel decoder to the embed_dim of transformer decoder.
            Defaults to False.
        transformer_decoder (:obj:`mmcv.ConfigDict` | dict): Config for
            transformer decoder. Defaults to None.
        positional_encoding (:obj:`mmcv.ConfigDict` | dict): Config for
            transformer decoder position encoding. Defaults to None.
        loss_cls (:obj:`mmcv.ConfigDict` | dict): Config of the classification
            loss. Defaults to None.
        loss_mask (:obj:`mmcv.ConfigDict` | dict): Config of the mask loss.
            Defaults to None.
        loss_dice (:obj:`mmcv.ConfigDict` | dict): Config of the dice loss.
            Defaults to None.
        train_cfg (:obj:`mmcv.ConfigDict` | dict): Training config of
            Mask2Former head.
        test_cfg (:obj:`mmcv.ConfigDict` | dict): Testing config of
            Mask2Former head.
        init_cfg (dict or list[dict], optional): Initialization config dict.
            Defaults to None.
    """

    def __init__(self,
                 in_channels,
                 feat_channels,
                 out_channels,
                 num_things_classes=80,
                 num_stuff_classes=53,
                 num_queries=100,
                 num_transformer_feat_level=3,
                 pixel_decoder=None,
                 enforce_decoder_input_project=False,
                 transformer_decoder=None,
                 positional_encoding=None,
                 loss_cls=None,
                 loss_mask=None,
                 loss_dice=None,
                 loss_mse=None,
                 loss_rank=None,
                 loss_domain=None,
                 train_cfg=None,
                 test_cfg=None,
                 init_cfg=None,
                 **kwargs):
        super(AnchorFreeHead, self).__init__(init_cfg)
        self.num_things_classes = num_things_classes
        self.num_stuff_classes = num_stuff_classes
        self.num_classes = self.num_things_classes + self.num_stuff_classes
        self.num_queries = num_queries
        self.num_transformer_feat_level = num_transformer_feat_level
        self.num_heads = transformer_decoder.transformerlayers.\
            attn_cfgs.num_heads
        self.num_transformer_decoder_layers = transformer_decoder.num_layers
        assert pixel_decoder.encoder.transformerlayers.\
            attn_cfgs.num_levels == num_transformer_feat_level
        pixel_decoder_ = copy.deepcopy(pixel_decoder)
        pixel_decoder_.update(
            in_channels=in_channels,
            feat_channels=feat_channels,
            out_channels=out_channels)
        self.pixel_decoder = build_plugin_layer(pixel_decoder_)[1]

        # build structure and texture transformer decoder
        self.structure_transformer_decoder = build_transformer_layer_sequence(
            transformer_decoder)
        self.texture_transformer_decoder = build_transformer_layer_sequence(
            transformer_decoder)
        self.decoder_embed_dims = self.structure_transformer_decoder.embed_dims

        self.decoder_input_projs = ModuleList()
        # from low resolution to high resolution
        for _ in range(num_transformer_feat_level):
            if (self.decoder_embed_dims != feat_channels
                    or enforce_decoder_input_project):
                self.decoder_input_projs.append(
                    Conv2d(
                        feat_channels, self.decoder_embed_dims, kernel_size=1))
            else:
                self.decoder_input_projs.append(nn.Identity())
        self.decoder_positional_encoding = build_positional_encoding(
            positional_encoding)

        self.structure_query_embed = nn.Embedding(self.num_queries, feat_channels)
        self.structure_query_feat = nn.Embedding(self.num_queries, feat_channels)
        self.texture_query_embed = nn.Embedding(self.num_queries, feat_channels)
        self.texture_query_feat = nn.Embedding(self.num_queries, feat_channels)
        # from low resolution to high resolution
        self.level_embed = nn.Embedding(self.num_transformer_feat_level,
                                        feat_channels)

        self.structure_cls_embed = nn.Linear(feat_channels, self.num_classes + 1)
        self.structure_rank_embed = nn.Linear(feat_channels, 1)
        self.structure_mask_embed = nn.Sequential(
            nn.Linear(feat_channels, feat_channels), nn.ReLU(inplace=True),
            nn.Linear(feat_channels, feat_channels), nn.ReLU(inplace=True),
            nn.Linear(feat_channels, out_channels))

        self.texture_cls_embed = nn.Linear(feat_channels, self.num_classes + 1)
        self.texture_rank_embed = nn.Linear(feat_channels, 1)
        self.texture_mask_embed = nn.Sequential(
            nn.Linear(feat_channels, feat_channels), nn.ReLU(inplace=True),
            nn.Linear(feat_channels, feat_channels), nn.ReLU(inplace=True),
            nn.Linear(feat_channels, out_channels))

        self.rank_head = DomainSepModule(feat_channels, num_queries)

        self.test_cfg = test_cfg
        self.train_cfg = train_cfg
        if train_cfg:
            # structure assigner & sampler
            self.structure_assigner = build_assigner(self.train_cfg.structure_assigner)
            self.structure_sampler = build_sampler(self.train_cfg.structure_sampler, context=self)
            # texture assigner & sampler
            self.texture_assigner = build_assigner(self.train_cfg.texture_assigner)
            self.texture_sampler = build_sampler(self.train_cfg.texture_sampler, context=self)
            # rank assigner & sampler
            self.rank_assigner = build_assigner(self.train_cfg.rank_assigner)
            # self.rank_sampler = build_sampler(self.train_cfg.rank_sampler, context=self)

            self.num_points = self.train_cfg.get('num_points', 12544)
            self.oversample_ratio = self.train_cfg.get('oversample_ratio', 3.0)
            self.importance_sample_ratio = self.train_cfg.get(
                'importance_sample_ratio', 0.75)

        self.class_weight = loss_cls.class_weight
        self.loss_cls = build_loss(loss_cls)
        self.loss_mask = build_loss(loss_mask)
        self.loss_dice = build_loss(loss_dice)
        self.loss_mse = build_loss(loss_mse)
        self.loss_rank = build_loss(loss_rank)
        self.loss_domain = build_loss(loss_domain)

    def init_weights(self):
        for m in self.decoder_input_projs:
            if isinstance(m, Conv2d):
                caffe2_xavier_init(m, bias=0)

        self.pixel_decoder.init_weights()

        for p in self.structure_transformer_decoder.parameters():
            if p.dim() > 1:
                nn.init.xavier_normal_(p)
        for p in self.texture_transformer_decoder.parameters():
            if p.dim() > 1:
                nn.init.xavier_normal_(p)

    def preprocess_gt(self, gt_labels_list, gt_masks_list, gt_semantic_segs,
                      img_metas):
        """Preprocess the ground truth for all images.

        Args:
            gt_labels_list (list[Tensor]): Each is ground truth
                labels of each bbox, with shape (num_gts, ).
            gt_masks_list (list[BitmapMasks]): Each is ground truth
                masks of each instances of a image, shape
                (num_gts, h, w).
            gt_semantic_seg (Tensor | None): Ground truth of semantic
                segmentation with the shape (batch_size, n, h, w).
                [0, num_thing_class - 1] means things,
                [num_thing_class, num_class-1] means stuff,
                255 means VOID. It's None when training instance segmentation.
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple: a tuple containing the following targets.
                - labels (list[Tensor]): Ground truth class indices\
                    for all images. Each with shape (n, ), n is the sum of\
                    number of stuff type and number of instance in a image.
                - masks (list[Tensor]): Ground truth mask for each\
                    image, each with shape (n, h, w).
        """
        num_things_list = [self.num_things_classes] * len(gt_labels_list)
        num_stuff_list = [self.num_stuff_classes] * len(gt_labels_list)
        if gt_semantic_segs is None:
            gt_semantic_segs = [None] * len(gt_labels_list)

        targets = multi_apply(preprocess_panoptic_gt, gt_labels_list,
                              gt_masks_list, gt_semantic_segs, num_things_list,
                              num_stuff_list, img_metas)
        labels, masks = targets
        return labels, masks

    def get_targets(self, cls_scores_list, mask_preds_list, gt_labels_list,
                    rank_labels_list, gt_masks_list, img_metas):
        """Compute classification and mask targets for all images for a decoder
        layer.

        Args:
            cls_scores_list (list[Tensor]): Mask score logits from a single
                decoder layer for all images. Each with shape (num_queries,
                cls_out_channels).
            mask_preds_list (list[Tensor]): Mask logits from a single decoder
                layer for all images. Each with shape (num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for all
                images. Each with shape (n, ), n is the sum of number of stuff
                type and number of instance in a image.
            gt_masks_list (list[Tensor]): Ground truth mask for each image,
                each with shape (n, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple[list[Tensor]]: a tuple containing the following targets.
                - labels_list (list[Tensor]): Labels of all images.\
                    Each with shape (num_queries, ).
                - label_weights_list (list[Tensor]): Label weights\
                    of all images. Each with shape (num_queries, ).
                - mask_targets_list (list[Tensor]): Mask targets of\
                    all images. Each with shape (num_queries, h, w).
                - mask_weights_list (list[Tensor]): Mask weights of\
                    all images. Each with shape (num_queries, ).
                - num_total_pos (int): Number of positive samples in\
                    all images.
                - num_total_neg (int): Number of negative samples in\
                    all images.
        """
        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         mask_targets_list, mask_weights_list,
         pos_inds_list, neg_inds_list) = multi_apply(self._get_target_single,
                                                     cls_scores_list, mask_preds_list,
                                                     gt_labels_list, rank_labels_list,
                                                     gt_masks_list, img_metas)

        num_total_pos = sum((inds.numel() for inds in pos_inds_list))
        num_total_neg = sum((inds.numel() for inds in neg_inds_list))
        return (labels_list, label_weights_list,
                ranks_list, rank_weights_list,
                mask_targets_list, mask_weights_list,
                num_total_pos, num_total_neg)

    def _get_target_single(self, cls_score, mask_pred,
                           gt_labels, rank_labels, gt_masks, img_metas):
        """Compute classification and mask targets for one image.

        Args:
            cls_score (Tensor): Mask score logits from a single decoder layer
                for one image. Shape (num_queries, cls_out_channels).
            mask_pred (Tensor): Mask logits for a single decoder layer for one
                image. Shape (num_queries, h, w).
            gt_labels (Tensor): Ground truth class indices for one image with
                shape (num_gts, ).
            gt_masks (Tensor): Ground truth mask for each image, each with
                shape (num_gts, h, w).
            img_metas (dict): Image informtation.

        Returns:
            tuple[Tensor]: A tuple containing the following for one image.

                - labels (Tensor): Labels of each image. \
                    shape (num_queries, ).
                - label_weights (Tensor): Label weights of each image. \
                    shape (num_queries, ).
                - mask_targets (Tensor): Mask targets of each image. \
                    shape (num_queries, h, w).
                - mask_weights (Tensor): Mask weights of each image. \
                    shape (num_queries, ).
                - pos_inds (Tensor): Sampled positive indices for each \
                    image.
                - neg_inds (Tensor): Sampled negative indices for each \
                    image.
        """
        # sample points
        num_queries = cls_score.shape[0]
        num_gts = gt_labels.shape[0]

        point_coords = torch.rand((1, self.num_points, 2),
                                  device=cls_score.device)
        # shape (num_queries, num_points)
        mask_points_pred = point_sample(
            mask_pred.unsqueeze(1), point_coords.repeat(num_queries, 1,
                                                        1)).squeeze(1)
        # shape (num_gts, num_points)
        gt_points_masks = point_sample(
            gt_masks.unsqueeze(1).float(), point_coords.repeat(num_gts, 1,
                                                               1)).squeeze(1)

        # assign and sample
        assign_result = self.structure_assigner.assign(cls_score, mask_points_pred,
                                             gt_labels, gt_points_masks,
                                             img_metas)
        sampling_result = self.structure_sampler.sample(assign_result, mask_pred,
                                              gt_masks)
        pos_inds = sampling_result.pos_inds
        neg_inds = sampling_result.neg_inds

        # label target
        labels = gt_labels.new_full((self.num_queries, ),
                                    self.num_classes,
                                    dtype=torch.long)

        labels[pos_inds] = gt_labels[sampling_result.pos_assigned_gt_inds]
        label_weights = gt_labels.new_ones((self.num_queries, ))

        # rank target
        ranks = rank_labels.new_full((self.num_queries, ),
                                     0,
                                     dtype=torch.float32)
        ranks[pos_inds] = rank_labels[sampling_result.pos_assigned_gt_inds]
        rank_weights = rank_labels.new_ones((self.num_queries, ))

        # mask target
        mask_targets = gt_masks[sampling_result.pos_assigned_gt_inds]
        mask_weights = mask_pred.new_zeros((self.num_queries, ))
        mask_weights[pos_inds] = 1.0

        return (labels, label_weights, ranks, rank_weights, mask_targets, mask_weights, pos_inds,
                neg_inds)

    def get_texture_targets(self, cls_scores_list, mask_preds_list, gt_labels_list,
                            rank_labels_list, gt_masks_list, img_metas):
        """Compute classification and mask targets for all images for a decoder
        layer.

        Args:
            cls_scores_list (list[Tensor]): Mask score logits from a single
                decoder layer for all images. Each with shape (num_queries,
                cls_out_channels).
            mask_preds_list (list[Tensor]): Mask logits from a single decoder
                layer for all images. Each with shape (num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for all
                images. Each with shape (n, ), n is the sum of number of stuff
                type and number of instance in a image.
            gt_masks_list (list[Tensor]): Ground truth mask for each image,
                each with shape (n, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple[list[Tensor]]: a tuple containing the following targets.
                - labels_list (list[Tensor]): Labels of all images.\
                    Each with shape (num_queries, ).
                - label_weights_list (list[Tensor]): Label weights\
                    of all images. Each with shape (num_queries, ).
                - mask_targets_list (list[Tensor]): Mask targets of\
                    all images. Each with shape (num_queries, h, w).
                - mask_weights_list (list[Tensor]): Mask weights of\
                    all images. Each with shape (num_queries, ).
                - num_total_pos (int): Number of positive samples in\
                    all images.
                - num_total_neg (int): Number of negative samples in\
                    all images.
        """
        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         mask_targets_list, mask_weights_list,
         pos_inds_list, neg_inds_list) = multi_apply(self._get_texture_target_single,
                                                     cls_scores_list, mask_preds_list,
                                                     gt_labels_list, rank_labels_list,
                                                     gt_masks_list, img_metas)

        num_total_pos = sum((inds.numel() for inds in pos_inds_list))
        num_total_neg = sum((inds.numel() for inds in neg_inds_list))
        return (labels_list, label_weights_list,
                ranks_list, rank_weights_list,
                mask_targets_list, mask_weights_list,
                num_total_pos, num_total_neg)

    def _get_texture_target_single(self, cls_score, mask_pred,
                                   gt_labels, rank_labels, gt_masks, img_metas):
        """Compute classification and mask targets for one image.

        Args:
            cls_score (Tensor): Mask score logits from a single decoder layer
                for one image. Shape (num_queries, cls_out_channels).
            mask_pred (Tensor): Mask logits for a single decoder layer for one
                image. Shape (num_queries, h, w).
            gt_labels (Tensor): Ground truth class indices for one image with
                shape (num_gts, ).
            gt_masks (Tensor): Ground truth mask for each image, each with
                shape (num_gts, h, w).
            img_metas (dict): Image informtation.

        Returns:
            tuple[Tensor]: A tuple containing the following for one image.

                - labels (Tensor): Labels of each image. \
                    shape (num_queries, ).
                - label_weights (Tensor): Label weights of each image. \
                    shape (num_queries, ).
                - mask_targets (Tensor): Mask targets of each image. \
                    shape (num_queries, h, w).
                - mask_weights (Tensor): Mask weights of each image. \
                    shape (num_queries, ).
                - pos_inds (Tensor): Sampled positive indices for each \
                    image.
                - neg_inds (Tensor): Sampled negative indices for each \
                    image.
        """
        # sample points
        num_queries = cls_score.shape[0]
        num_gts = gt_labels.shape[0]

        point_coords = torch.rand((1, self.num_points, 2),
                                  device=cls_score.device)
        # shape (num_queries, 3, num_points)
        mask_points_pred = point_sample(
            mask_pred, point_coords.repeat(num_queries, 1, 1))
        # shape (num_gts, 3, num_points)
        gt_points_masks = point_sample(
            gt_masks.float(), point_coords.repeat(num_gts, 1, 1))

        # assign and sample
        assign_result = self.texture_assigner.assign(cls_score, mask_points_pred,
                                                     gt_labels, gt_points_masks,
                                                     img_metas)
        sampling_result = self.texture_sampler.sample(assign_result, mask_pred,
                                                      gt_masks)
        pos_inds = sampling_result.pos_inds
        neg_inds = sampling_result.neg_inds

        # label target
        labels = gt_labels.new_full((self.num_queries, ),
                                    self.num_classes,
                                    dtype=torch.long)

        labels[pos_inds] = gt_labels[sampling_result.pos_assigned_gt_inds]
        label_weights = gt_labels.new_ones((self.num_queries, ))

        # rank target
        ranks = rank_labels.new_full((self.num_queries, ),
                                     0,
                                     dtype=torch.float32)
        ranks[pos_inds] = rank_labels[sampling_result.pos_assigned_gt_inds]
        rank_weights = rank_labels.new_ones((self.num_queries, ))

        # mask target
        mask_targets = gt_masks[sampling_result.pos_assigned_gt_inds]
        mask_weights = mask_pred.new_zeros((self.num_queries, ))
        mask_weights[pos_inds] = 1.0

        return (labels, label_weights, ranks, rank_weights, mask_targets, mask_weights, pos_inds,
                neg_inds)

    def get_domain_targets(self, cls_scores_list, gt_labels_list,
                           rank_labels_list, img_metas):
        """Compute classification and mask targets for all images for a decoder
        layer.

        Args:
            cls_scores_list (list[Tensor]): Mask score logits from a single
                decoder layer for all images. Each with shape (num_queries,
                cls_out_channels).
            mask_preds_list (list[Tensor]): Mask logits from a single decoder
                layer for all images. Each with shape (num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for all
                images. Each with shape (n, ), n is the sum of number of stuff
                type and number of instance in a image.
            gt_masks_list (list[Tensor]): Ground truth mask for each image,
                each with shape (n, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple[list[Tensor]]: a tuple containing the following targets.
                - labels_list (list[Tensor]): Labels of all images.\
                    Each with shape (num_queries, ).
                - label_weights_list (list[Tensor]): Label weights\
                    of all images. Each with shape (num_queries, ).
                - mask_targets_list (list[Tensor]): Mask targets of\
                    all images. Each with shape (num_queries, h, w).
                - mask_weights_list (list[Tensor]): Mask weights of\
                    all images. Each with shape (num_queries, ).
                - num_total_pos (int): Number of positive samples in\
                    all images.
                - num_total_neg (int): Number of negative samples in\
                    all images.
        """
        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         pos_inds_list, neg_inds_list) = multi_apply(self._get_domain_target_single,
                                                     cls_scores_list, gt_labels_list,
                                                     rank_labels_list, img_metas)

        num_total_pos = sum((inds.numel() for inds in pos_inds_list))
        num_total_neg = sum((inds.numel() for inds in neg_inds_list))
        return (labels_list, label_weights_list,
                ranks_list, rank_weights_list,
                num_total_pos, num_total_neg)

    def _get_domain_target_single(self, cls_score,
                                  gt_labels, rank_labels, img_metas):
        """Compute classification and mask targets for one image.

        Args:
            cls_score (Tensor): Mask score logits from a single decoder layer
                for one image. Shape (num_queries, cls_out_channels).
            mask_pred (Tensor): Mask logits for a single decoder layer for one
                image. Shape (num_queries, h, w).
            gt_labels (Tensor): Ground truth class indices for one image with
                shape (num_gts, ).
            gt_masks (Tensor): Ground truth mask for each image, each with
                shape (num_gts, h, w).
            img_metas (dict): Image informtation.

        Returns:
            tuple[Tensor]: A tuple containing the following for one image.

                - labels (Tensor): Labels of each image. \
                    shape (num_queries, ).
                - label_weights (Tensor): Label weights of each image. \
                    shape (num_queries, ).
                - mask_targets (Tensor): Mask targets of each image. \
                    shape (num_queries, h, w).
                - mask_weights (Tensor): Mask weights of each image. \
                    shape (num_queries, ).
                - pos_inds (Tensor): Sampled positive indices for each \
                    image.
                - neg_inds (Tensor): Sampled negative indices for each \
                    image.
        """

        # assign and sample
        assign_result = self.rank_assigner.assign(cls_score, gt_labels, img_metas)

        pos_inds = torch.nonzero(
            assign_result.gt_inds > 0, as_tuple=False).squeeze(-1).unique()
        neg_inds = torch.nonzero(
            assign_result.gt_inds == 0, as_tuple=False).squeeze(-1).unique()
        pos_assigned_gt_inds = assign_result.gt_inds[pos_inds] - 1
        # label target
        labels = gt_labels.new_full((self.num_queries, ),
                                    self.num_classes,
                                    dtype=torch.long)
        labels[pos_inds] = gt_labels[pos_assigned_gt_inds]
        label_weights = gt_labels.new_ones((self.num_queries, ))

        # rank target
        ranks = rank_labels.new_full((self.num_queries, ),
                                     0,
                                     dtype=torch.float32)
        ranks[pos_inds] = rank_labels[pos_assigned_gt_inds]
        rank_weights = rank_labels.new_ones((self.num_queries, ))

        return (labels, label_weights, ranks, rank_weights, pos_inds,
                neg_inds)

    def structure_loss_single(self, cls_scores, rank_scores, mask_preds,
                              gt_labels_list, rank_labels_list,
                              gt_masks_list, img_metas):
        """Loss function for outputs from a single decoder layer.

        Args:
            cls_scores (Tensor): Mask score logits from a single decoder layer
                for all images. Shape (batch_size, num_queries,
                cls_out_channels). Note `cls_out_channels` should includes
                background.
            mask_preds (Tensor): Mask logits for a pixel decoder for all
                images. Shape (batch_size, num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for each
                image, each with shape (num_gts, ).
            gt_masks_list (list[Tensor]): Ground truth mask for each image,
                each with shape (num_gts, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple[Tensor]: Loss components for outputs from a single \
                decoder layer.
        """
        num_imgs = cls_scores.size(0)
        cls_scores_list = [cls_scores[i] for i in range(num_imgs)]
        mask_preds_list = [mask_preds[i] for i in range(num_imgs)]

        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         mask_targets_list, mask_weights_list,
         num_total_pos, num_total_neg) = \
            self.get_targets(cls_scores_list, mask_preds_list,
                             gt_labels_list, rank_labels_list, gt_masks_list, img_metas)

        # shape (batch_size, num_queries)
        labels = torch.stack(labels_list, dim=0)
        # shape (batch_size, num_queries)
        label_weights = torch.stack(label_weights_list, dim=0)

        # shape (batch_size, num_queries)
        ranks = torch.stack(ranks_list, dim=0)
        # shape (batch_size, num_queries)
        rank_weights = torch.stack(rank_weights_list, dim=0)

        # shape (num_total_gts, h, w)
        mask_targets = torch.cat(mask_targets_list, dim=0)
        # shape (batch_size, num_queries)
        mask_weights = torch.stack(mask_weights_list, dim=0)

        # classfication loss
        # shape (batch_size * num_queries, )
        cls_scores = cls_scores.flatten(0, 1)
        labels = labels.flatten(0, 1)
        label_weights = label_weights.flatten(0, 1)

        class_weight = cls_scores.new_tensor(self.class_weight)
        loss_cls = self.loss_cls(
            cls_scores,
            labels,
            label_weights,
            avg_factor=class_weight[labels].sum())

        # relation loss
        # shape (batch_size, num_queries)
        loss_rank = self.loss_rank(ranks, rank_scores)

        num_total_masks = reduce_mean(cls_scores.new_tensor([num_total_pos]))
        num_total_masks = max(num_total_masks, 1)

        # extract positive ones
        # shape (batch_size, num_queries, h, w) -> (num_total_gts, h, w)
        mask_preds = mask_preds[mask_weights > 0]

        if mask_targets.shape[0] == 0:
            # zero match
            loss_dice = mask_preds.sum()
            loss_mask = mask_preds.sum()
            return loss_cls, loss_rank, loss_mask, loss_dice

        with torch.no_grad():
            points_coords = get_uncertain_point_coords_with_randomness(
                mask_preds.unsqueeze(1), None, self.num_points,
                self.oversample_ratio, self.importance_sample_ratio)
            # shape (num_total_gts, h, w) -> (num_total_gts, num_points)
            mask_point_targets = point_sample(
                mask_targets.unsqueeze(1).float(), points_coords).squeeze(1)
        # shape (num_queries, h, w) -> (num_queries, num_points)
        mask_point_preds = point_sample(
            mask_preds.unsqueeze(1), points_coords).squeeze(1)

        # dice loss
        loss_dice = self.loss_dice(
            mask_point_preds, mask_point_targets, avg_factor=num_total_masks)

        # mask loss
        # shape (num_queries, num_points) -> (num_queries * num_points, )
        mask_point_preds = mask_point_preds.reshape(-1)
        # shape (num_total_gts, num_points) -> (num_total_gts * num_points, )
        mask_point_targets = mask_point_targets.reshape(-1)
        loss_mask = self.loss_mask(
            mask_point_preds,
            mask_point_targets,
            avg_factor=num_total_masks * self.num_points)

        return loss_cls, loss_rank, loss_mask, loss_dice

    def get_texture_labels(self, img_list, gt_masks_list):
        """

        Args:
            img_list(list[Tensor]): each with shape (3, h, w).
            gt_masks_list(list[Tensor]): each with shape (num_gts, h, w).

        Returns(list[Tensor]): each with shape (num_gts, 3, h, w).

        """
        texture_labels_list = []
        for i in range(len(img_list)):
            img = img_list[i]
            masks = gt_masks_list[i]
            text_objs = []
            for obj in masks:
                text_obj = obj * img
                text_objs.append(text_obj)
            if len(text_objs) > 0:
                text_objs = torch.stack(text_objs, dim=0)
            else:
                text_objs = masks.unsqueeze(1).repeat(1, 3, 1, 1)
            texture_labels_list.append(text_objs)
        return texture_labels_list

    def texture_loss_single(self, cls_scores, rank_scores, mask_preds_exp, mask_preds, #TODO
                            gt_labels_list, rank_labels_list,
                            gt_masks_list, img_list, img_metas):
        """Loss function for outputs from a single decoder layer.

        Args:
            cls_scores (Tensor): Mask score logits from a single decoder layer
                for all images. Shape (batch_size, num_queries,
                cls_out_channels). Note `cls_out_channels` should includes
                background.
            mask_preds (Tensor): Mask logits for a pixel decoder for all
                images. Shape (batch_size, num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for each
                image, each with shape (num_gts, ).
            gt_masks_list (list[Tensor]): Ground truth mask for each image,
                each with shape (num_gts, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            tuple[Tensor]: Loss components for outputs from a single \
                decoder layer.
        """
        num_imgs = cls_scores.size(0)
        cls_scores_list = [cls_scores[i] for i in range(num_imgs)]
        mask_preds_list = [mask_preds_exp[i] for i in range(num_imgs)] #TODO
        # texture_labels_list = self.get_texture_labels(img_list, gt_masks_list)

        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         mask_targets_list, mask_weights_list,
         num_total_pos, num_total_neg) = \
            self.get_targets(cls_scores_list, mask_preds_list, # TODO
                             gt_labels_list, rank_labels_list, gt_masks_list, img_metas)
            # self.get_texture_targets(cls_scores_list, mask_preds_list,
            #                          gt_labels_list, rank_labels_list, texture_labels_list, img_metas)

        # shape (batch_size, num_queries)
        labels = torch.stack(labels_list, dim=0)
        # shape (batch_size, num_queries)
        label_weights = torch.stack(label_weights_list, dim=0)

        # shape (batch_size, num_queries)
        ranks = torch.stack(ranks_list, dim=0)
        # shape (batch_size, num_queries)
        rank_weights = torch.stack(rank_weights_list, dim=0)

        # shape (num_total_gts, h, w)
        mask_targets_list = self.get_texture_labels(img_list, mask_targets_list) #TODO
        mask_targets = torch.cat(mask_targets_list, dim=0)
        # shape (batch_size, num_queries)
        mask_weights = torch.stack(mask_weights_list, dim=0)

        # classfication loss
        # shape (batch_size * num_queries, )
        cls_scores = cls_scores.flatten(0, 1)
        labels = labels.flatten(0, 1)
        label_weights = label_weights.flatten(0, 1)

        class_weight = cls_scores.new_tensor(self.class_weight)
        loss_cls = self.loss_cls(
            cls_scores,
            labels,
            label_weights,
            avg_factor=class_weight[labels].sum())

        # relation loss
        # shape (batch_size, num_queries)
        loss_rank = self.loss_rank(ranks, rank_scores)

        num_total_masks = reduce_mean(cls_scores.new_tensor([num_total_pos]))
        num_total_masks = max(num_total_masks, 1)

        # extract positive ones
        # shape (batch_size, num_queries, 3, h, w) -> (num_total_gts, 3, h, w)
        mask_preds = mask_preds[mask_weights > 0]
        mask_preds = mask_preds.view(-1, mask_preds.size(-2), mask_preds.size(-1))

        if mask_targets.shape[0] == 0:
            # zero match
            loss_mse = mask_preds.sum()
            return loss_cls, loss_rank, loss_mse

        with torch.no_grad():
            points_coords = get_uncertain_point_coords_with_randomness(
                mask_preds.unsqueeze(1), None, self.num_points,
                self.oversample_ratio, self.importance_sample_ratio)

            # shape (num_total_gts, h, w) -> (num_total_gts, num_points)
            mask_targets = mask_targets.view(-1, mask_targets.size(-2), mask_targets.size(-1))
            mask_point_targets = point_sample(
                mask_targets.unsqueeze(1).float(), points_coords).squeeze(1)
        # shape (num_queries, h, w) -> (num_queries, num_points)
        mask_point_preds = point_sample(
            mask_preds.unsqueeze(1), points_coords).squeeze(1)

        # mse loss
        # shape (num_queries, num_points) -> (num_queries * num_points, )
        mask_point_preds = mask_point_preds.reshape(-1)
        # shape (num_total_gts, num_points) -> (num_total_gts * num_points, )
        mask_point_targets = mask_point_targets.reshape(-1)
        loss_mse = self.loss_mse(
            mask_point_preds,
            mask_point_targets,
            avg_factor=num_total_masks * self.num_points)

        return loss_cls, loss_rank, loss_mse

    def node_loss(self, rank_results, mask_preds, gt_labels_list, rank_labels_list, gt_masks_list, img_metas): #TODO
        cls_pred_lf = rank_results['cls_pred_lf']
        cls_pred_hf = rank_results['cls_pred_hf']
        cls_pred = rank_results['cls_pred']

        rank_pred_lf = rank_results['rank_pred_lf']
        rank_pred_hf = rank_results['rank_pred_hf']
        rank_pred = rank_results['rank_pred']

        num_imgs = cls_pred.size(0)
        cls_pred_lf_list = [cls_pred_lf[i] for i in range(num_imgs)]
        cls_pred_hf_list = [cls_pred_hf[i] for i in range(num_imgs)]
        cls_pred_list = [cls_pred[i] for i in range(num_imgs)]
        mask_preds_list = [mask_preds[i] for i in range(num_imgs)]  # TODO

        # calculate lf class and rank loss
        # (labels_lf_list, label_weights_lf_list,
        #  ranks_lf_list, rank_weights_lf_list,
        #  num_total_lf_pos, num_total_lf_neg) = \
        #     self.get_domain_targets(cls_pred_lf_list, gt_labels_list,
        #                             rank_labels_list, img_metas)
        (labels_lf_list, label_weights_lf_list,
         ranks_lf_list, rank_weights_lf_list,
         mask_targets_list, mask_weights_list,
         num_total_lf_pos, num_total_lf_neg) = \
            self.get_targets(cls_pred_lf_list, mask_preds_list, # TODO
                             gt_labels_list, rank_labels_list, gt_masks_list, img_metas)

        labels_lf = torch.stack(labels_lf_list, dim=0)
        label_weights_lf = torch.stack(label_weights_lf_list, dim=0)

        ranks_lf = torch.stack(ranks_lf_list, dim=0)
        rank_weights_lf = torch.stack(rank_weights_lf_list, dim=0)

        cls_pred_lf = cls_pred_lf.flatten(0, 1)
        labels_lf = labels_lf.flatten(0, 1)
        label_weights_lf = label_weights_lf.flatten(0, 1)

        class_weight_lf = cls_pred_lf.new_tensor(self.class_weight)
        loss_cls_lf = self.loss_cls(
            cls_pred_lf,
            labels_lf,
            label_weights_lf,
            avg_factor=class_weight_lf[labels_lf].sum())

        loss_rank_lf = self.loss_rank(ranks_lf, rank_pred_lf)

        # calculate hf class and rank loss
        # (labels_hf_list, label_weights_hf_list,
        #  ranks_hf_list, rank_weights_hf_list,
        #  num_total_hf_pos, num_total_hf_neg) = \
        #     self.get_domain_targets(cls_pred_hf_list, gt_labels_list,
        #                             rank_labels_list, img_metas)
        (labels_hf_list, label_weights_hf_list,
         ranks_hf_list, rank_weights_hf_list,
         mask_targets_list, mask_weights_list,
         num_total_hf_pos, num_total_hf_neg) = \
            self.get_targets(cls_pred_hf_list, mask_preds_list, # TODO
                             gt_labels_list, rank_labels_list, gt_masks_list, img_metas)

        labels_hf = torch.stack(labels_hf_list, dim=0)
        label_weights_hf = torch.stack(label_weights_hf_list, dim=0)

        ranks_hf = torch.stack(ranks_hf_list, dim=0)
        rank_weights_hf = torch.stack(rank_weights_hf_list, dim=0)

        cls_pred_hf = cls_pred_hf.flatten(0, 1)
        labels_hf = labels_hf.flatten(0, 1)
        label_weights_hf = label_weights_hf.flatten(0, 1)

        class_weight_hf = cls_pred_hf.new_tensor(self.class_weight)
        loss_cls_hf = self.loss_cls(
            cls_pred_hf,
            labels_hf,
            label_weights_hf,
            avg_factor=class_weight_hf[labels_hf].sum())

        loss_rank_hf = self.loss_rank(ranks_hf, rank_pred_hf)

        # calculate final class and rank loss
        # (labels_list, label_weights_list,
        #  ranks_list, rank_weights_list,
        #  num_total_pos, num_total_neg) = \
        #     self.get_domain_targets(cls_pred_list, gt_labels_list,
        #                             rank_labels_list, img_metas)
        (labels_list, label_weights_list,
         ranks_list, rank_weights_list,
         mask_targets_list, mask_weights_list,
         num_total_pos, num_total_neg) = \
            self.get_targets(cls_pred_list, mask_preds_list, # TODO
                             gt_labels_list, rank_labels_list, gt_masks_list, img_metas)

        labels = torch.stack(labels_list, dim=0)
        label_weights = torch.stack(label_weights_list, dim=0)

        ranks = torch.stack(ranks_list, dim=0)
        rank_weights = torch.stack(rank_weights_list, dim=0)

        cls_pred = cls_pred.flatten(0, 1)
        labels = labels.flatten(0, 1)
        label_weights = label_weights.flatten(0, 1)

        class_weight = cls_pred.new_tensor(self.class_weight)
        loss_cls = self.loss_cls(
            cls_pred,
            labels,
            label_weights,
            avg_factor=class_weight[labels].sum())

        loss_rank = self.loss_rank(ranks, rank_pred)

        loss_domain = self.loss_domain(rank_results, ranks)

        loss = loss_cls_lf + loss_rank_lf + loss_cls_hf + loss_rank_hf + loss_cls + loss_rank + loss_domain

        return loss,

    @force_fp32(apply_to=('structure_cls_scores', 'structure_rank_scores', 'structure_mask_preds',
                          'texture_cls_scores', 'texture_rank_scores', 'texture_mask_preds', 'rank_results_list'))
    def loss(self, structure_cls_scores, structure_rank_scores, structure_mask_preds,
             texture_cls_scores, texture_rank_scores, texture_mask_preds, rank_results_list,
             gt_labels_list, gt_masks_list, rank_labels_list, img, img_metas):
        """Loss function.

        Args:
            all_cls_scores (Tensor): Classification scores for all decoder
                layers with shape (num_decoder, batch_size, num_queries,
                cls_out_channels). Note `cls_out_channels` should includes
                background.
            all_mask_preds (Tensor): Mask scores for all decoder layers with
                shape (num_decoder, batch_size, num_queries, h, w).
            gt_labels_list (list[Tensor]): Ground truth class indices for each
                image with shape (n, ). n is the sum of number of stuff type
                and number of instance in a image.
            gt_masks_list (list[Tensor]): Ground truth mask for each image with
                shape (n, h, w).
            img_metas (list[dict]): List of image meta information.

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """

        num_dec_layers = len(structure_cls_scores)
        all_gt_labels_list = [gt_labels_list for _ in range(num_dec_layers)]
        all_rank_labels_list = [rank_labels_list for _ in range(num_dec_layers)]
        all_gt_masks_list = [gt_masks_list for _ in range(num_dec_layers)]
        img_metas_list = [img_metas for _ in range(num_dec_layers)]
        img_list = [img for _ in range(num_dec_layers)]

        # losses_structure_cls, losses_structure_rank, \
        # losses_structure_mask, losses_structure_dice = multi_apply(
        #     self.structure_loss_single,
        #     structure_cls_scores, structure_rank_scores, structure_mask_preds,
        #     all_gt_labels_list, all_rank_labels_list, all_gt_masks_list, img_metas_list)
        #
        # losses_texture_cls, losses_texture_rank, losses_texture_mse = multi_apply(
        #     self.texture_loss_single,
        #     texture_cls_scores, texture_rank_scores, texture_mask_preds,
        #     all_gt_labels_list, all_rank_labels_list, all_gt_masks_list, img_list, img_metas_list)
        #
        # losses_domain = self.node_loss(rank_results, gt_labels_list, rank_labels_list, img_metas)

        losses_structure_cls, losses_structure_rank, \
        losses_structure_mask, losses_structure_dice = multi_apply(
            self.structure_loss_single,
            structure_cls_scores, structure_rank_scores, structure_mask_preds,
            all_gt_labels_list, all_rank_labels_list, all_gt_masks_list, img_metas_list)

        losses_texture_cls, losses_texture_rank, losses_texture_mse = multi_apply(
            self.texture_loss_single,
            texture_cls_scores, texture_rank_scores, structure_mask_preds, texture_mask_preds,
            all_gt_labels_list, all_rank_labels_list, all_gt_masks_list, img_list, img_metas_list)

        losses_domain, = multi_apply(
            self.node_loss, rank_results_list, structure_mask_preds,
            all_gt_labels_list, all_rank_labels_list, all_gt_masks_list, img_metas_list
        )

        loss_dict = dict()
        # loss from the last decoder layer
        loss_dict['losses_structure_cls'] = losses_structure_cls[-1]
        loss_dict['losses_structure_mask'] = losses_structure_mask[-1]
        loss_dict['losses_structure_dice'] = losses_structure_dice[-1]
        loss_dict['losses_structure_rank'] = losses_structure_rank[-1]

        loss_dict['losses_texture_cls'] = losses_texture_cls[-1]
        loss_dict['losses_texture_rank'] = losses_texture_rank[-1]
        loss_dict['losses_texture_mse'] = losses_texture_mse[-1]

        loss_dict['losses_domain'] = losses_domain[-1]

        # loss from other decoder layers
        num_dec_layer = 0
        for loss_st_cls_i, loss_st_mask_i, loss_st_dice_i, loss_st_rank_i, \
            loss_te_cls_i, loss_te_mse_i, loss_te_rank_i, loss_domain_i in zip(
                losses_structure_cls[:-1], losses_structure_mask[:-1],
                losses_structure_dice[:-1], losses_structure_rank[:-1],
                losses_texture_cls[:-1], losses_texture_mse[:-1], losses_texture_rank[:-1], losses_domain[:-1]):
            loss_dict[f'd{num_dec_layer}.losses_structure_cls'] = loss_st_cls_i
            loss_dict[f'd{num_dec_layer}.losses_structure_mask'] = loss_st_mask_i
            loss_dict[f'd{num_dec_layer}.losses_structure_dice'] = loss_st_dice_i
            loss_dict[f'd{num_dec_layer}.losses_structure_rank'] = loss_st_rank_i
            loss_dict[f'd{num_dec_layer}.losses_texture_cls'] = loss_te_cls_i
            loss_dict[f'd{num_dec_layer}.losses_texture_mse'] = loss_te_mse_i
            loss_dict[f'd{num_dec_layer}.losses_texture_rank'] = loss_te_rank_i
            loss_dict[f'd{num_dec_layer}.losses_domain'] = loss_domain_i
            num_dec_layer += 1

        return loss_dict

    def forward_structure_head(self, decoder_out, mask_feature, attn_mask_target_size):
        """Forward for head part which is called after every decoder layer.

        Args:
            decoder_out (Tensor): in shape (num_queries, batch_size, c).
            mask_feature (Tensor): in shape (batch_size, c, h, w).
            attn_mask_target_size (tuple[int, int]): target attention
                mask size.

        Returns:
            tuple: A tuple contain three elements.

            - cls_pred (Tensor): Classification scores in shape \
                (batch_size, num_queries, cls_out_channels). \
                Note `cls_out_channels` should includes background.
            - mask_pred (Tensor): Mask scores in shape \
                (batch_size, num_queries,h, w).
            - attn_mask (Tensor): Attention mask in shape \
                (batch_size * num_heads, num_queries, h, w).
        """
        decoder_out = self.structure_transformer_decoder.post_norm(decoder_out)
        decoder_out = decoder_out.transpose(0, 1)
        # shape (batch_size, num_queries, c)
        cls_pred = self.structure_cls_embed(decoder_out)
        rank_pred = self.structure_rank_embed(decoder_out).squeeze(-1)
        # shape (batch_size, num_queries, c)
        mask_embed = self.structure_mask_embed(decoder_out)
        # shape (batch_size, num_queries, h, w)
        mask_pred = torch.einsum('bqc,bchw->bqhw', mask_embed, mask_feature)
        attn_mask = F.interpolate(
            mask_pred,
            attn_mask_target_size,
            mode='bilinear',
            align_corners=False)
        # shape (batch_size, num_queries, h, w) ->
        #   (batch_size * num_head, num_queries, h*w)
        attn_mask = attn_mask.flatten(2).unsqueeze(1).repeat(
            (1, self.num_heads, 1, 1)).flatten(0, 1)
        attn_mask = attn_mask.sigmoid() < 0.5
        attn_mask = attn_mask.detach()

        return cls_pred, rank_pred, mask_pred, attn_mask

    def forward_texture_head(self, decoder_out, mask_feature, attn_mask_target_size):
        """Forward for head part which is called after every decoder layer.

        Args:
            decoder_out (Tensor): in shape (num_queries, batch_size, c).
            mask_feature (Tensor): in shape (batch_size, c, h, w).
            attn_mask_target_size (tuple[int, int]): target attention
                mask size.

        Returns:
            tuple: A tuple contain three elements.

            - cls_pred (Tensor): Classification scores in shape \
                (batch_size, num_queries, cls_out_channels). \
                Note `cls_out_channels` should includes background.
            - mask_pred (Tensor): Mask scores in shape \
                (batch_size, num_queries,h, w).
            - attn_mask (Tensor): Attention mask in shape \
                (batch_size * num_heads, num_queries, h, w).
        """
        decoder_out = self.texture_transformer_decoder.post_norm(decoder_out)
        decoder_out = decoder_out.transpose(0, 1)
        # shape (batch_size, num_queries, c)
        cls_pred = self.texture_cls_embed(decoder_out)
        rank_pred = self.texture_rank_embed(decoder_out).squeeze(-1)
        # shape (batch_size, num_queries, c)
        mask_embed = self.texture_mask_embed(decoder_out)
        # shape (batch_size, c, h, w) -> (batch_size, c, 3, h, w)
        B, C, H, W = mask_feature.shape
        mask_feature = mask_feature.repeat(1, 3, 1, 1).view(B, C, -1, H, W)
        # shape (batch_size, num_queries, h, w)
        mask_pred = torch.einsum('bqc,bcrhw->bqrhw', mask_embed, mask_feature)

        return cls_pred, rank_pred, mask_pred

    def forward_gnn_sep_head(self, structure_feat, texture_feat):
        rank_results = self.rank_head(structure_feat, texture_feat)
        return rank_results

    def forward(self, feats, img_metas):
        """Forward function.

        Args:
            feats (list[Tensor]): Multi scale Features from the
                upstream network, each is a 4D-tensor.
            img_metas (list[dict]): List of image information.

        Returns:
            tuple: A tuple contains two elements.

            - cls_pred_list (list[Tensor)]: Classification logits \
                for each decoder layer. Each is a 3D-tensor with shape \
                (batch_size, num_queries, cls_out_channels). \
                Note `cls_out_channels` should includes background.
            - mask_pred_list (list[Tensor]): Mask logits for each \
                decoder layer. Each with shape (batch_size, num_queries, \
                 h, w).
        """
        batch_size = len(img_metas)
        mask_features, multi_scale_memorys = self.pixel_decoder(feats)
        # multi_scale_memorys (from low resolution to high resolution)
        decoder_inputs = []
        decoder_positional_encodings = []
        for i in range(self.num_transformer_feat_level):
            decoder_input = self.decoder_input_projs[i](multi_scale_memorys[i])
            # shape (batch_size, c, h, w) -> (h*w, batch_size, c)
            decoder_input = decoder_input.flatten(2).permute(2, 0, 1)
            level_embed = self.level_embed.weight[i].view(1, 1, -1)
            decoder_input = decoder_input + level_embed
            # shape (batch_size, c, h, w) -> (h*w, batch_size, c)
            mask = decoder_input.new_zeros(
                (batch_size, ) + multi_scale_memorys[i].shape[-2:],
                dtype=torch.bool)
            decoder_positional_encoding = self.decoder_positional_encoding(
                mask)
            decoder_positional_encoding = decoder_positional_encoding.flatten(
                2).permute(2, 0, 1)
            decoder_inputs.append(decoder_input)
            decoder_positional_encodings.append(decoder_positional_encoding)

        # shape (num_queries, c) -> (num_queries, batch_size, c)
        # 初始化结构和纹理query_feat
        structure_query_feat = self.structure_query_feat.weight.unsqueeze(1).repeat(
            (1, batch_size, 1))
        structure_query_embed = self.structure_query_embed.weight.unsqueeze(1).repeat(
            (1, batch_size, 1))

        texture_query_feat = self.texture_query_feat.weight.unsqueeze(1).repeat(
            (1, batch_size, 1))
        texture_query_embed = self.texture_query_embed.weight.unsqueeze(1).repeat(
            (1, batch_size, 1))

        structure_cls_pred_list = []
        structure_rank_pred_list = []
        structure_mask_pred_list = []
        texture_cls_pred_list = []
        texture_rank_pred_list = []
        texture_mask_pred_list = []
        rank_results_list = []
        structure_cls_pred, structure_rank_pred, structure_mask_pred, attn_mask = self.forward_structure_head(
            structure_query_feat, mask_features, multi_scale_memorys[0].shape[-2:])

        texture_cls_pred, texture_rank_pred, texture_mask_pred = self.forward_texture_head(
            texture_query_feat, mask_features, multi_scale_memorys[0].shape[-2:])

        rank_results = self.forward_gnn_sep_head(
            structure_query_feat.transpose(0, 1), texture_query_feat.transpose(0, 1))

        structure_cls_pred_list.append(structure_cls_pred)
        structure_rank_pred_list.append(structure_rank_pred)
        structure_mask_pred_list.append(structure_mask_pred)
        texture_cls_pred_list.append(texture_cls_pred)
        texture_rank_pred_list.append(texture_rank_pred)
        texture_mask_pred_list.append(texture_mask_pred)
        rank_results_list.append(rank_results)

        for i in range(self.num_transformer_decoder_layers):
            level_idx = i % self.num_transformer_feat_level
            # if a mask is all True(all background), then set it all False.
            attn_mask[torch.where(
                attn_mask.sum(-1) == attn_mask.shape[-1])] = False

            # cross_attn + self_attn
            structure_layer = self.structure_transformer_decoder.layers[i]
            texture_layer = self.texture_transformer_decoder.layers[i]
            attn_masks = [attn_mask, None]

            structure_query_feat = structure_layer(
                query=structure_query_feat,
                key=decoder_inputs[level_idx],
                value=decoder_inputs[level_idx],
                query_pos=structure_query_embed,
                key_pos=decoder_positional_encodings[level_idx],
                attn_masks=attn_masks,
                query_key_padding_mask=None,
                # here we do not apply masking on padded region
                key_padding_mask=None)

            texture_query_feat = texture_layer(
                query=texture_query_feat,
                key=decoder_inputs[level_idx],
                value=decoder_inputs[level_idx],
                query_pos=texture_query_embed,
                key_pos=decoder_positional_encodings[level_idx],
                attn_masks=attn_masks,
                query_key_padding_mask=None,
                # here we do not apply masking on padded region
                key_padding_mask=None)

            structure_cls_pred, structure_rank_pred, structure_mask_pred, attn_mask = self.forward_structure_head(
                structure_query_feat, mask_features, multi_scale_memorys[
                    (i + 1) % self.num_transformer_feat_level].shape[-2:])

            texture_cls_pred, texture_rank_pred, texture_mask_pred = self.forward_texture_head(
                texture_query_feat, mask_features, multi_scale_memorys[
                    (i + 1) % self.num_transformer_feat_level].shape[-2:])

            rank_results = self.forward_gnn_sep_head(
                structure_query_feat.transpose(0, 1), texture_query_feat.transpose(0, 1))

            structure_cls_pred_list.append(structure_cls_pred)
            structure_rank_pred_list.append(structure_rank_pred)
            structure_mask_pred_list.append(structure_mask_pred)
            texture_cls_pred_list.append(texture_cls_pred)
            texture_rank_pred_list.append(texture_rank_pred)
            texture_mask_pred_list.append(texture_mask_pred)
            rank_results_list.append(rank_results)

        return structure_cls_pred_list, structure_rank_pred_list, structure_mask_pred_list, \
               texture_cls_pred_list, texture_rank_pred_list, texture_mask_pred_list, rank_results_list

    def forward_train(self,
                      feats,
                      img,
                      img_metas,
                      gt_bboxes,
                      gt_labels,
                      gt_masks,
                      gt_ranks,  # TODO
                      gt_semantic_seg,
                      gt_bboxes_ignore=None):
        """Forward function for training mode.

        Args:
            feats (list[Tensor]): Multi-level features from the upstream
                network, each is a 4D-tensor.
            img_metas (list[Dict]): List of image information.
            gt_bboxes (list[Tensor]): Each element is ground truth bboxes of
                the image, shape (num_gts, 4). Not used here.
            gt_labels (list[Tensor]): Each element is ground truth labels of
                each box, shape (num_gts,).
            gt_masks (list[BitmapMasks]): Each element is masks of instances
                of a image, shape (num_gts, h, w).
            gt_semantic_seg (list[tensor] | None): Each element is the ground
                truth of semantic segmentation with the shape (N, H, W).
                [0, num_thing_class - 1] means things,
                [num_thing_class, num_class-1] means stuff,
                255 means VOID. It's None when training instance segmentation.
            gt_bboxes_ignore (list[Tensor]): Ground truth bboxes to be
                ignored. Defaults to None.

        Returns:
            dict[str, Tensor]: a dictionary of loss components
        """
        # not consider ignoring bboxes
        assert gt_bboxes_ignore is None

        # forward
        structure_cls_pred_list, structure_rank_pred_list, structure_mask_pred_list, \
        texture_cls_pred_list, texture_rank_pred_list, texture_mask_pred_list, \
        rank_results_list = self(feats, img_metas)

        # preprocess ground truth
        gt_labels, gt_masks = self.preprocess_gt(gt_labels, gt_masks,
                                                 gt_semantic_seg, img_metas)

        # loss
        losses = self.loss(structure_cls_pred_list, structure_rank_pred_list, structure_mask_pred_list,
                           texture_cls_pred_list, texture_rank_pred_list, texture_mask_pred_list,
                           rank_results_list, gt_labels, gt_masks, gt_ranks, img, img_metas)

        return losses

    def simple_test(self, feats, img_metas, **kwargs):
        """Test without augmentaton.

        Args:
            feats (list[Tensor]): Multi-level features from the
                upstream network, each is a 4D-tensor.
            img_metas (list[dict]): List of image information.

        Returns:
            tuple: A tuple contains two tensors.

            - mask_cls_results (Tensor): Mask classification logits,\
                shape (batch_size, num_queries, cls_out_channels).
                Note `cls_out_channels` should includes background.
            - mask_pred_results (Tensor): Mask logits, shape \
                (batch_size, num_queries, h, w).
        """
        structure_cls_pred_list, structure_rank_pred_list, structure_mask_pred_list, \
        texture_cls_pred_list, texture_rank_pred_list, texture_mask_pred_list, \
        rank_results_list = self(feats, img_metas)

        # rank_pred = structure_rank_pred_list[-1]
        rank_pred = rank_results_list[-1]['rank_pred']
        cls_pred = rank_results_list[-1]['cls_pred']

        mask_pred_results = structure_mask_pred_list[-1]
        # upsample masks
        img_shape = img_metas[0]['batch_input_shape']
        mask_pred_results = F.interpolate(
            mask_pred_results,
            size=(img_shape[0], img_shape[1]),
            mode='bilinear',
            align_corners=False)

        # batch_salient_indx = []
        # for cp in cls_pred:
        #     scores = F.softmax(cp, dim=-1)[:, :-1]
        #     scores_per_image = scores.flatten(0, 1)
        #     salient_obj = torch.zeros(self.num_queries)
        #     salient_obj[scores_per_image > 0.4] = 1
        #     batch_salient_indx.append(salient_obj)
        # salient_indx = torch.stack(batch_salient_indx, dim=0)
        #
        # if len(torch.unique(salient_indx)) == 1:
        #     return rank_pred, mask_pred_results
        # sal_rank = []
        # sal_mask = []
        # for idx, sal in enumerate(salient_indx.flatten(0, 1)):
        #     if sal == 1:
        #         sal_rank.append(rank_pred[:, idx])
        #         sal_mask.append(mask_pred_results[:, idx, ...])
        #
        # rank_pred = torch.stack(sal_rank, dim=1)
        # mask_pred_results = torch.stack(sal_mask, dim=1)

        # import cv2
        # text_pred_results = texture_mask_pred_list[-1]
        # text_pred_result = F.interpolate(
        #     text_pred_results[0],
        #     size=(img_shape[0], img_shape[1]),
        #     mode='bilinear',
        #     align_corners=False)
        # for i in range(len(text_pred_result)):
        #     text = text_pred_result[i].permute(1, 2, 0).cpu().detach().numpy()
        #     cv2.imwrite("/opt/data/private/mmdetection/work_dirs/rgb_test/{}.png".format(i),
        #                  text * [58.395, 57.12, 57.375] + [123.675, 116.28, 103.53])
        # raise

        # import cv2
        # text_pred_results = structure_mask_pred_list[-1]
        # text_pred_result = F.interpolate(
        #     text_pred_results[0].unsqueeze(1),
        #     size=(img_shape[0], img_shape[1]),
        #     mode='bilinear',
        #     align_corners=False)
        # for i in range(len(text_pred_result)):
        #     text = text_pred_result[i].permute(1, 2, 0).cpu().detach().numpy()
        #     cv2.imwrite("/opt/data/private/mmdetection/work_dirs/rgb_test/{}.png".format(i),
        #                  text * 255)
        # raise
        return rank_pred, mask_pred_results


#Used in stage 1 (ANFL)
def normalize_digraph(A):
    b, n, _ = A.shape
    node_degrees = A.detach().sum(dim = -1)
    degs_inv_sqrt = node_degrees ** -0.5
    norm_degs_matrix = torch.eye(n)
    dev = A.get_device()
    if dev >= 0:
        norm_degs_matrix = norm_degs_matrix.to(dev)
    norm_degs_matrix = norm_degs_matrix.view(1, n, n) * degs_inv_sqrt.view(b, n, 1)
    norm_A = torch.bmm(torch.bmm(norm_degs_matrix, A), norm_degs_matrix)
    return norm_A


#Used in stage 2 (MEFL)
def create_e_matrix(n):
    end = torch.zeros((n*n,n))
    for i in range(n):
        end[i * n:(i + 1) * n, i] = 1
    start = torch.zeros(n, n)
    for i in range(n):
        start[i, i] = 1
    start = start.repeat(n, 1)
    return start, end


def bn_init(bn):
    bn.weight.data.fill_(1)
    bn.bias.data.zero_()


class LinearBlock(nn.Module):
    def __init__(self, in_features, out_features=None, drop=0.01, norm_cfg=dict(type='LN')):
        super().__init__()
        out_features = out_features or in_features
        self.fc = nn.Linear(in_features, out_features)
        self.bn = build_norm_layer(norm_cfg, out_features)[1]
        self.relu = nn.ReLU(inplace=True)
        self.drop = nn.Dropout(drop)

        self.fc.weight.data.normal_(0, math.sqrt(2. / out_features))
        self.bn.weight.data.fill_(1)
        self.bn.bias.data.zero_()

    def forward(self, x):
        x = self.drop(x)
        x = self.fc(x)
        x = self.relu(self.bn(x))
        return x


class FFM(nn.Module):
    def __init__(self, d_in, d_out, expansion_rate=4, drop=0.2, activation=F.relu, layer_norm_eps=1e-5):
        super(FFM, self).__init__()
        self.linear1 = nn.Linear(d_in, d_in*expansion_rate)
        self.dropout = nn.Dropout(drop)
        self.linear2 = nn.Linear(d_in*expansion_rate, d_out)
        self.norm = nn.LayerNorm(d_out, eps=layer_norm_eps)
        self.dropout1 = nn.Dropout(drop)

        self.activation = activation

        if d_in != d_out:
            self.res = nn.Linear(d_in, d_out)
        else:
            self.res = nn.Identity()

    def forward(self, x):
        res = self.res(x)
        x = self.norm(res + self._ff_block(x))
        return x

    # feed forward block
    def _ff_block(self, x: torch.Tensor) -> torch.Tensor:
        x = self.linear2(self.dropout(self.activation(self.linear1(x))))
        return self.dropout1(x)


# Gated GCN Used to Learn Multi-dimensional Edge Features and Node Features
class GNN_EDGE(nn.Module):
    def __init__(self, in_channels, num_nodes, norm_cfg=dict(type='LN')):
        super(GNN_EDGE, self).__init__()
        self.in_channels = in_channels
        self.num_nodes = num_nodes
        # GNN Matrix: E x N
        # Start Matrix Item:  define the source node of one edge
        # End Matrix Item:  define the target node of one edge
        # Algorithm details in Residual Gated Graph Convnets: arXiv preprint arXiv:1711.07553
        # or Benchmarking Graph Neural Networks: arXiv preprint arXiv:2003.00982v3

        start, end = create_e_matrix(self.num_nodes)
        self.start = Variable(start, requires_grad=False)
        self.end = Variable(end, requires_grad=False)

        dim_in = self.in_channels
        dim_out = self.in_channels

        self.U1 = nn.Linear(dim_in, dim_out, bias=False)
        self.V1 = nn.Linear(dim_in, dim_out, bias=False)
        self.A1 = nn.Linear(dim_in, dim_out, bias=False)
        self.B1 = nn.Linear(dim_in, dim_out, bias=False)
        self.E1 = nn.Linear(dim_in, dim_out, bias=False)

        self.U2 = nn.Linear(dim_in, dim_out, bias=False)
        self.V2 = nn.Linear(dim_in, dim_out, bias=False)
        self.A2 = nn.Linear(dim_in, dim_out, bias=False)
        self.B2 = nn.Linear(dim_in, dim_out, bias=False)
        self.E2 = nn.Linear(dim_in, dim_out, bias=False)

        self.sigmoid = nn.Sigmoid()
        self.softmax = nn.Softmax(2)
        self.bnv1 = build_norm_layer(norm_cfg, dim_out)[1]
        self.bne1 = build_norm_layer(norm_cfg, dim_out)[1]
        # self.bne1 = nn.BatchNorm1d(num_nodes*num_nodes)

        self.bnv2 = build_norm_layer(norm_cfg, dim_out)[1]
        self.bne2 = build_norm_layer(norm_cfg, dim_out)[1]
        # self.bnv2 = nn.BatchNorm1d(num_nodes)
        # self.bne2 = nn.BatchNorm1d(num_nodes * num_nodes)

        self.act = nn.ReLU()

        self.init_weights_linear(dim_in, 1)

    def init_weights_linear(self, dim_in, gain):
        # conv1
        scale = gain * np.sqrt(2.0 / dim_in)
        self.U1.weight.data.normal_(0, scale)
        self.V1.weight.data.normal_(0, scale)
        self.A1.weight.data.normal_(0, scale)
        self.B1.weight.data.normal_(0, scale)
        self.E1.weight.data.normal_(0, scale)

        self.U2.weight.data.normal_(0, scale)
        self.V2.weight.data.normal_(0, scale)
        self.A2.weight.data.normal_(0, scale)
        self.B2.weight.data.normal_(0, scale)
        self.E2.weight.data.normal_(0, scale)

        bn_init(self.bnv1)
        bn_init(self.bne1)
        bn_init(self.bnv2)
        bn_init(self.bne2)

    def forward(self, x, edge):
        # device
        dev = x.get_device()
        if dev >= 0:
            start = self.start.to(dev)
            end = self.end.to(dev)

        # GNN Layer 1:
        res = x
        Vix = self.A1(x)  # V x d_out
        Vjx = self.B1(x)  # V x d_out
        e = self.E1(edge)  # E x d_out
        edge = edge + self.act(self.bne1(torch.einsum('ev, bvc -> bec', (end, Vix)) + torch.einsum('ev, bvc -> bec',(start, Vjx)) + e))  # E x d_out

        e = self.sigmoid(edge)
        b, _, c = e.shape
        e = e.view(b, self.num_nodes, self.num_nodes, c)
        e = self.softmax(e)
        e = e.view(b, -1, c)

        Ujx = self.V1(x)  # V x H_out
        Ujx = torch.einsum('ev, bvc -> bec', (start, Ujx))  # E x H_out
        Uix = self.U1(x)  # V x H_out
        x = Uix + torch.einsum('ve, bec -> bvc', (end.t(), e * Ujx)) / self.num_nodes  # V x H_out
        x = self.act(res + self.bnv1(x))
        res = x

        # GNN Layer 2:
        Vix = self.A2(x)  # V x d_out
        Vjx = self.B2(x)  # V x d_out
        e = self.E2(edge)  # E x d_out
        edge = edge + self.act(self.bne2(torch.einsum('ev, bvc -> bec', (end, Vix)) + torch.einsum('ev, bvc -> bec', (start, Vjx)) + e))  # E x d_out

        e = self.sigmoid(edge)
        b, _, c = e.shape
        e = e.view(b, self.num_nodes, self.num_nodes, c)
        e = self.softmax(e)
        e = e.view(b, -1, c)

        Ujx = self.V2(x)  # V x H_out
        Ujx = torch.einsum('ev, bvc -> bec', (start, Ujx))  # E x H_out
        Uix = self.U2(x)  # V x H_out
        x = Uix + torch.einsum('ve, bec -> bvc', (end.t(), e * Ujx)) / self.num_nodes  # V x H_out
        x = self.act(res + self.bnv2(x))
        return x, edge


class GEM(nn.Module):
    def __init__(self, in_channels):
        super(GEM, self).__init__()
        self.FAM = nn.MultiheadAttention(embed_dim=in_channels, num_heads=8, batch_first=True)
        self.ARM = nn.MultiheadAttention(embed_dim=in_channels, num_heads=8, batch_first=True)

        self.edge_proj = FFM(in_channels, in_channels)

    def forward(self, class_feature, global_feature):
        B, N, D = class_feature.shape
        global_feature = global_feature.repeat(1, N, 1).view(B, N, D)
        feat = self.FAM(class_feature, global_feature, global_feature, need_weights=False)[0]

        feat_end = feat.repeat(1, 1, N).view(B, -1, D)
        feat_start = feat.repeat(1, N, 1).view(B, -1, D)
        feat = self.ARM(feat_start, feat_end, feat_end, need_weights=False)[0]

        edge = self.edge_proj(feat)
        return edge


'''
Domain Separation Module
GNNEncoder:
DomainDecoder:
DomainSepModule:
'''


class GFEM(nn.Module):
    def __init__(self, in_dim, num_nodes, norm_cfg=dict(type='SyncBN')):
        super(GFEM, self).__init__()

        self.gfe1 = nn.Conv1d(in_dim, 4 * in_dim, kernel_size=num_nodes, padding=0)
        self.norm1 = build_norm_layer(norm_cfg, 4 * in_dim)[1]

        self.gfe2 = nn.Conv1d(4 * in_dim, in_dim, kernel_size=1, padding=0)
        self.norm2 = build_norm_layer(norm_cfg, in_dim)[1]

        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        """

        Args:
            x[Tensor]: shape[batch, num_nodes, c]

        Returns: global feature with shape [batch, 1, c]

        """
        x = x.permute(0, 2, 1)
        gf = self.act(self.norm1(self.gfe1(x)))
        gf = self.act(self.norm2(self.gfe2(gf)))
        return gf.permute(0, 2, 1).contiguous()


class GNNEncoder(nn.Module):
    def __init__(self, in_dim, num_nodes, norm_cfg=dict(type='SyncBN')):
        super(GNNEncoder, self).__init__()
        self.num_nodes = num_nodes
        self.layers = GNN_EDGE(in_dim, self.num_nodes)
        self.global_layer = GFEM(in_dim, num_nodes, norm_cfg=norm_cfg)
        self.edge_extractor = GEM(in_dim)

        self.ffm = FFM(in_dim, in_dim)

    def forward(self, x):
        res = x
        global_feature = self.global_layer(x)
        f_e = self.edge_extractor(x, global_feature)
        f_v, f_e = self.layers(x, f_e)
        f_v = f_v + res

        f_v = self.ffm(f_v)
        return f_v


class GraphSepModule(nn.Module):
    def __init__(self, in_dim, num_nodes, norm_cfg=dict(type='GN', num_groups=8)):
        super(GraphSepModule, self).__init__()
        self.squ = LinearBlock(in_dim, 32)
        self.enc = GNNEncoder(in_dim=32, num_nodes=num_nodes, norm_cfg=norm_cfg)

    def forward(self, x):
        x = self.squ(x)
        sep_node = self.enc(x)
        noi_node = x - sep_node
        gnn_sep_results = dict(
            sep_node=sep_node,
            noi_node=noi_node
        )
        return gnn_sep_results


'''
norm_cfg = dict(type='GN', num_groups=32)
'''
class DomainSepModule(nn.Module):
    def __init__(self, in_dim, num_nodes, norm_cfg=dict(type='GN', num_groups=32)):
        super(DomainSepModule, self).__init__()

        self.in_dim = in_dim

        self.hf_rank_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)
        self.lf_rank_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)

        self.shared_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)

        self.lf_shared_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)
        self.hf_shared_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)
        self.hf_private_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)
        self.lf_private_gnn_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)

        self.shared_gnn_dec = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)

        self.rank_embed = LinearBlock(in_dim*2, in_dim, drop=0.)
        self.restore_hf = LinearBlock(in_dim*2, in_dim, drop=0.)
        self.restore_lf = LinearBlock(in_dim*2, in_dim, drop=0.)

        # self.final_enc = GNNEncoder(in_dim=in_dim, num_nodes=num_nodes, norm_cfg=norm_cfg)
        self.gnn_sep = GraphSepModule(in_dim=in_dim, num_nodes=num_nodes)

        self.rank_head_hf = nn.Linear(in_dim, 1)
        self.rank_head_lf = nn.Linear(in_dim, 1)
        self.rank_head_final = nn.Linear(32, 1)

        self.cls_head_hf = nn.Linear(in_dim, 2)
        self.cls_head_lf = nn.Linear(in_dim, 2)
        self.cls_head_final = nn.Linear(32, 2)

        self._init_weights()

    def _init_weights(self):
        self.rank_head_lf.weight.data.normal_(0, 0.02)
        self.rank_head_hf.weight.data.normal_(0, 0.02)
        self.rank_head_final.weight.data.normal_(0, 0.02)

    def forward(self, structure_feature, texture_feature):

        hf_feature = self.hf_rank_gnn_enc(structure_feature)
        lf_feature = self.lf_rank_gnn_enc(texture_feature)

        hf_shared_feature = self.shared_gnn_enc(hf_feature)
        lf_shared_feature = self.shared_gnn_enc(lf_feature)

        hf_private_feature = self.hf_private_gnn_enc(hf_feature)
        lf_private_feature = self.lf_private_gnn_enc(lf_feature)

        restored_hf_feature = self.restore_hf(torch.cat([hf_private_feature, hf_shared_feature], dim=-1))
        restored_lf_feature = self.restore_lf(torch.cat([lf_private_feature, lf_shared_feature], dim=-1))
        restored_hf_feature = self.shared_gnn_dec(restored_hf_feature)  # TODO: concat
        restored_lf_feature = self.shared_gnn_dec(restored_lf_feature)

        lf_shared_feature = self.lf_shared_gnn_enc(lf_shared_feature)
        hf_shared_feature = self.hf_shared_gnn_enc(hf_shared_feature)

        rank_pred_lf = self.rank_head_hf(lf_shared_feature).squeeze(-1)
        rank_pred_hf = self.rank_head_lf(hf_shared_feature).squeeze(-1)

        cls_pred_lf = self.cls_head_lf(lf_shared_feature)
        cls_pred_hf = self.cls_head_hf(hf_shared_feature)

        shared_feature = self.rank_embed(torch.cat([lf_shared_feature, hf_shared_feature], dim=-1))
        gnn_sep_results = self.gnn_sep(shared_feature)
        pred_feature = gnn_sep_results['sep_node']

        rank_pred = self.rank_head_final(pred_feature).squeeze(-1)
        cls_pred = self.cls_head_final(pred_feature)

        results = dict(
            hf_feature=hf_feature,
            lf_feature=lf_feature,
            hf_shared_feature=hf_shared_feature,
            lf_shared_feature=lf_shared_feature,
            hf_private_feature=hf_private_feature,
            lf_private_feature=lf_private_feature,
            restored_hf_feature=restored_hf_feature,
            restored_lf_feature=restored_lf_feature,
            rank_pred_lf=rank_pred_lf,
            rank_pred_hf=rank_pred_hf,
            pred_feature=pred_feature,
            rank_pred=rank_pred,
            cls_pred_lf=cls_pred_lf,
            cls_pred_hf=cls_pred_hf,
            cls_pred=cls_pred,
            gnn_sep_results=gnn_sep_results)

        # import cv2
        # hf_mask = results['lf_mask'][0]
        # for i in range(hf_mask.shape[0]):
        #     # cv2.imwrite("/opt/data/private/mmdetection/work_dirs/test/{}.png".format(i), hf_mask[i].cpu().detach().numpy()*255)
        #     recon_mask = hf_mask[i].permute(1, 2, 0).cpu().detach().numpy()
        #     recon_mask = recon_mask[..., ::-1]
        #     print(np.unique(recon_mask * [58.395, 57.12, 57.375] + [123.675, 116.28, 103.53]))
        #     cv2.imwrite("/opt/data/private/mmdetection/work_dirs/test/{}.png".format(i),
        #                 recon_mask * [58.395, 57.12, 57.375] + [123.675, 116.28, 103.53])
        # raise
        return results
