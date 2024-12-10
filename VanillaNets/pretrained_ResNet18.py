# Modification of hypnettorch file
# https://hypnettorch.readthedocs.io/en/latest/_modules/hypnettorch/mnets/resnet_imgnet.html#ResNetIN
# The structure of ResNet18 should be possibly the same like in other publications to make
# a fair comparison between continual learning methods.

from hypnettorch.mnets.classifier_interface import Classifier
from hypnettorch.mnets.mlp import MLP
from hypnettorch.mnets.wide_resnet import WRN

from torchvision.models import resnet18
import torch.nn as nn


class PretrainedResNet18(Classifier):
    """
    ResNet-18 with weigths pretrained on ImageNet dataset. The weights are applied to a feature extractor part.
    However, a hypernetwork generates weights to a classification linear head.
    Right now, only images with input shape (224, 224, 3) are applicable.
    """

    def __init__(
        self,
        in_shape=(224, 224, 3),
        num_classes=40,
        no_weights=True,
        verbose=True,
        **kwargs
    ):
        super(PretrainedResNet18, self).__init__(num_classes, verbose)

        assert no_weights, "Weights should be entirely generated by a hypernetwork!"
        assert in_shape == (224,224,3), "Please reshape your data!"

        self.feature_extractor = resnet18(pretrained=True)
        self.feature_extractor = nn.Sequential(*list(self.feature_extractor.children())[:-1])
        self._in_shape = (in_shape[2], in_shape[0], in_shape[1])

        for param in self.feature_extractor.parameters():
            param.requires_grad_(False)

        self.linear_head = MLP(
            n_in=512,
            n_out=num_classes,
            hidden_layers=(),
            no_weights=no_weights,
            dropout_rate=-1,
            bn_track_stats=False,
            verbose=False
        )

        self._param_shapes = self.linear_head.param_shapes

        if verbose:
            print(f"Creating ResNet-18 model with weights pretrained on ImageNet.")


    def forward(self, x, weights=None, distilled_params=None, condition=None):
        """Compute the output :math:`y` of this network given the input
        :math:`x`.

        Parameters:
        -----------
            (....): See docstring of method
                :meth:`mnets.resnet.ResNet.forward`. We provide some more
                specific information below.
            x: torch.Tensor
                Based on the constructor argument ``chw_input_format``, either a flattened image batch with
                encoding ``HWC`` or an unflattened image batch with encoding
                ``CHW`` is expected.

        Returns:
        --------
            (torch.Tensor): The output of the network.
        """

        x = x.reshape((-1, *self._in_shape))
        
        # Forward pass through feature extractor
        x = self.feature_extractor(x)
        x = x.flatten(start_dim=1)

        # Forward pass through linear head
        x = self.linear_head.forward(x=x, weights=weights)

        return x
    

    def distillation_targets(self):
        """Targets to be distilled after training.

        See docstring of abstract super method
        :meth:`mnets.mnet_interface.MainNetInterface.distillation_targets`.

        This method will return the current batch statistics of all batch
        normalization layers if ``distill_bn_stats`` and ``use_batch_norm``
        were set to ``True`` in the constructor.

        Returns:
        --------
            The target tensors corresponding to the shapes specified in
            attribute :attr:`hyper_shapes_distilled`.
        """
        if self.hyper_shapes_distilled is None:
            return None

        ret = []
        for bn_layer in self._batchnorm_layers:
            ret.extend(bn_layer.get_stats())

        return ret

    def _compute_layer_out_sizes(self):
        """Compute the output shapes of all layers in this network excluding
        skip connection layers.

        This method will compute the output shape of each layer in this network,
        including the output layer, which just corresponds to the number of
        classes.

        Returns:
        ---------
            (list): A list of shapes (lists of integers). The first entry will
            correspond to the shape of the output of the first convolutional
            layer. The last entry will correspond to the output shape.

            .. note:
                Output shapes of convolutional layers will adhere PyTorch
                convention, i.e., ``[C, H, W]``, where ``C`` denotes the channel
                dimension.
        """
        in_shape = self._in_shape
        fs = self._filter_sizes
        init_ks = self._init_kernel_size
        stride_init = self._init_stride
        pd_init = self._init_padding

        # Note, `in_shape` is in Tensorflow layout.
        assert len(in_shape) == 3
        in_shape = [in_shape[2], *in_shape[:2]]

        ret = []

        C, H, W = in_shape

        # Recall the formular for convolutional layers:
        # W_new = (W - K + 2P) // S + 1

        # First conv layer.
        C = fs[0]
        H = (H - init_ks[0] + 2 * pd_init) // stride_init + 1
        W = (W - init_ks[1] + 2 * pd_init) // stride_init + 1
        ret.append([C, H, W])

        def add_block(H, W, C, stride):
            if self._bottleneck_blocks:
                H = (H - 1 + 2 * 0) // stride + 1
                W = (W - 1 + 2 * 0) // stride + 1
                ret.append([C, H, W])

                H = (H - 3 + 2 * 1) // 1 + 1
                W = (W - 3 + 2 * 1) // 1 + 1
                ret.append([C, H, W])

                C = 4 * C
                H = (H - 1 + 2 * 0) // 1 + 1
                W = (W - 1 + 2 * 0) // 1 + 1
                ret.append([C, H, W])

            else:
                H = (H - 3 + 2 * 1) // stride + 1
                W = (W - 3 + 2 * 1) // stride + 1
                ret.append([C, H, W])

                H = (H - 3 + 2 * 1) // 1 + 1
                W = (W - 3 + 2 * 1) // 1 + 1
                ret.append([C, H, W])

            return H, W, C

        # Group conv2_x
        if not self._cutout_mod:  # Max-pooling layer.
            H = (H - 3 + 2 * 1) // 2 + 1
            W = (W - 3 + 2 * 1) // 2 + 1

        for b in range(self._num_blocks[0]):
            H, W, C = add_block(H, W, fs[1], 1)

        # Group conv3_x
        for b in range(self._num_blocks[1]):
            H, W, C = add_block(H, W, fs[2], 2 if b == 0 else 1)

        # Group conv4_x
        for b in range(self._num_blocks[2]):
            H, W, C = add_block(H, W, fs[3], 2 if b == 0 else 1)

        # Group conv5_x
        for b in range(self._num_blocks[3]):
            H, W, C = add_block(H, W, fs[4], 2 if b == 0 else 1)

        # Final fully-connected layer (after avg pooling), i.e., output size.
        ret.append([self._num_classes])

        return ret

    def get_output_weight_mask(self, out_inds=None, device=None):
        """Create a mask for selecting weights connected solely to certain
        output units.

        See docstring of overwritten super method
        :meth:`mnets.mnet_interface.MainNetInterface.get_output_weight_mask`.
        """
        return WRN.get_output_weight_mask(self, out_inds=out_inds, device=device)

if __name__ == "__main__":
    pass