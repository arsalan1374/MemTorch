import math

import numpy as np
import torch
import torch.nn as nn

import memtorch
from memtorch.bh.crossbar.Crossbar import init_crossbar, simulate_matmul
from memtorch.bh.crossbar.Tile import gen_tiles, tile_matmul
from memtorch.map.Module import naive_tune
from memtorch.map.Parameter import naive_map
from memtorch.utils import convert_range, pad_tensor


class Conv3d(nn.Conv3d):
    """nn.Conv3d equivalent.

    Parameters
    ----------
    convolutional_layer : torch.nn.Conv3d
        Convolutional layer to patch.
    memristor_model : memtorch.bh.memristor.Memristor.Memristor
        Memristor model.
    memristor_model_params : **kwargs
        Memristor model keyword arguments.
    mapping_routine : function
        Mapping routine to use.
    transistor : bool
        Used to determine if a 1T1R (True) or 1R arrangement (False) is simulated.
    programming_routine : function
        Programming routine to use.
    programming_routine_params : **kwargs
        Programming routine keyword arguments.
    p_l: float
        If not None, the proportion of weights to retain.
    scheme : memtorch.bh.Scheme
        Weight representation scheme.
    tile_shape : (int, int)
        Tile shape to use to store weights. If None, modular tiles are not used.
    max_input_voltage : float
        Maximum input voltage used to encode inputs. If None, inputs are unbounded.
    ADC_resolution : int
        ADC resolution (bit width). If None, quantization noise is not accounted for.
    ADC_overflow_rate : float
        Overflow rate threshold for linear quanitzation (if ADC_resolution is not None).
    quant_method: string
        Quantization method. Must be in ['linear', 'log', 'log_minmax', 'minmax', 'tanh'], or None.
    use_bindings : bool
        Used to determine if C++/CUDA bindings are used (True) or not (False).
    verbose : bool
        Used to determine if verbose output is enabled (True) or disabled (False).
    """

    def __init__(
        self,
        convolutional_layer,
        memristor_model,
        memristor_model_params,
        mapping_routine=naive_map,
        transistor=True,
        programming_routine=None,
        programming_routine_params={},
        p_l=None,
        scheme=memtorch.bh.Scheme.DoubleColumn,
        tile_shape=None,
        max_input_voltage=None,
        ADC_resolution=None,
        ADC_overflow_rate=0.0,
        quant_method=None,
        use_bindings=True,
        verbose=True,
        *args,
        **kwargs
    ):
        assert isinstance(
            convolutional_layer, nn.Conv3d
        ), "convolutional_layer is not an instance of nn.Conv3d."
        self.device = torch.device("cpu" if "cpu" in memtorch.__version__ else "cuda")
        self.scheme = scheme
        self.tile_shape = tile_shape
        self.max_input_voltage = max_input_voltage
        self.ADC_resolution = ADC_resolution
        self.ADC_overflow_rate = ADC_overflow_rate
        if quant_method in memtorch.bh.Quantize.quant_methods:
            self.quant_method = quant_method
        else:
            self.quant_method = None

        if quant_method is not None:
            assert (
                ADC_resolution is not None
                and type(ADC_resolution) == int
                and ADC_resolution > 0
            ), "ADC resolution is invalid."
            assert (
                ADC_overflow_rate is not None
            ), "ADC_overflow_rate must be specified if quant_method is not None."

        self.use_bindings = use_bindings
        self.verbose = verbose
        self.forward_legacy_enabled = True
        super(Conv3d, self).__init__(
            convolutional_layer.in_channels,
            convolutional_layer.out_channels,
            convolutional_layer.kernel_size,
            **kwargs
        )
        self.padding = convolutional_layer.padding
        self.stride = convolutional_layer.stride
        self.weight.data = convolutional_layer.weight.data
        if convolutional_layer.bias is not None:
            self.bias.data = convolutional_layer.bias.data

        self.zero_grad()
        self.weight.requires_grad = False
        if convolutional_layer.bias is not None:
            self.bias.requires_grad = False

        self.crossbars, self.crossbar_operation = init_crossbar(
            weights=self.weight,
            memristor_model=memristor_model,
            memristor_model_params=memristor_model_params,
            transistor=transistor,
            mapping_routine=mapping_routine,
            programming_routine=programming_routine,
            programming_routine_params=programming_routine_params,
            p_l=p_l,
            scheme=scheme,
            tile_shape=tile_shape,
            use_bindings=use_bindings,
        )
        self.transform_output = lambda x: x
        if verbose:
            print("Patched %s -> %s" % (convolutional_layer, self))

    def forward(self, input):
        """Method to perform forward propagations.

        Parameters
        ----------
        input : torch.Tensor
            Input tensor.

        Returns
        -------
        torch.Tensor
            Output tensor.
        """
        if self.forward_legacy_enabled:
            return torch.nn.functional.conv3d(
                input.to(self.device),
                self.weight.to(self.device),
                bias=self.bias,
                stride=self.stride,
                padding=self.padding,
            )
        else:
            output_dim = [0, 0, 0]
            output_dim[0] = (
                int(
                    (input.shape[2] - self.kernel_size[0] + 2 * self.padding[0])
                    / self.stride[0]
                )
                + 1
            )
            output_dim[1] = (
                int(
                    (input.shape[3] - self.kernel_size[1] + 2 * self.padding[1])
                    / self.stride[1]
                )
                + 1
            )
            output_dim[2] = (
                int(
                    (input.shape[4] - self.kernel_size[2] + 2 * self.padding[2])
                    / self.stride[2]
                )
                + 1
            )
            out = torch.zeros(
                (
                    input.shape[0],
                    self.out_channels,
                    output_dim[0],
                    output_dim[1],
                    output_dim[2],
                )
            ).to(self.device)
            for batch in range(input.shape[0]):
                if not all(item == 0 for item in self.padding):
                    batch_input = nn.functional.pad(
                        input[batch],
                        pad=(
                            self.padding[2],
                            self.padding[2],
                            self.padding[1],
                            self.padding[1],
                            self.padding[0],
                            self.padding[0],
                        ),
                    )
                else:
                    batch_input = input[batch]

                if self.max_input_voltage is not None:
                    assert (
                        type(self.max_input_voltage) == int
                        or type(self.max_input_voltage) == float
                    ) and self.max_input_voltage > 0, (
                        "The maximum input voltage (max_input_voltage) must be >0."
                    )
                    batch_input = convert_range(
                        batch_input,
                        batch_input.min(),
                        batch_input.max(),
                        -self.max_input_voltage,
                        self.max_input_voltage,
                    )

                unfolded_batch_input = (
                    batch_input.unfold(1, self.kernel_size[0], self.stride[0])
                    .unfold(2, self.kernel_size[1], self.stride[1])
                    .unfold(3, self.kernel_size[2], self.stride[2])
                    .permute(1, 2, 3, 0, 4, 5, 6)
                    .reshape(
                        -1,
                        self.in_channels
                        * self.kernel_size[0]
                        * self.kernel_size[1]
                        * self.kernel_size[2],
                    )
                )
                unfolded_batch_input_shape = unfolded_batch_input.shape
                if hasattr(self, "non_linear"):
                    if self.tile_shape is not None:
                        tiles_map = self.crossbars[0].tiles_map
                        crossbar_shape = (
                            self.crossbars[0].rows,
                            self.crossbars[0].columns,
                        )
                    else:
                        tiles_map = None
                        crossbar_shape = None

                    if hasattr(self, "simulate"):
                        nl = False
                    else:
                        nl = True

                    out_ = (
                        self.crossbar_operation(
                            self.crossbars,
                            lambda crossbar, input_: simulate_matmul(
                                unfolded_batch_input,
                                crossbar,
                                nl=nl,
                                tiles_map=tiles_map,
                                crossbar_shape=crossbar_shape,
                                max_input_voltage=self.max_input_voltage,
                                ADC_resolution=self.ADC_resolution,
                                ADC_overflow_rate=self.ADC_overflow_rate,
                                quant_method=self.quant_method,
                                use_bindings=self.use_bindings,
                            ),
                            input_=unfolded_batch_input,
                        )
                        .to(self.device)
                        .T
                    )
                else:
                    if self.tile_shape is not None:
                        (
                            unfolded_batch_input_tiles,
                            unfolded_batch_input_tiles_map,
                        ) = gen_tiles(
                            unfolded_batch_input,
                            self.tile_shape,
                            input=True,
                            use_bindings=self.use_bindings,
                        )
                        crossbar_shape = (
                            self.crossbars[0].rows,
                            self.crossbars[0].columns,
                        )
                        tiles_map = self.crossbars[0].tiles_map
                        out_ = tile_matmul(
                            unfolded_batch_input_tiles,
                            unfolded_batch_input_tiles_map,
                            unfolded_batch_input_shape,
                            self.crossbar_operation(
                                self.crossbars,
                                lambda crossbar: crossbar.conductance_matrix,
                            ),
                            tiles_map,
                            crossbar_shape,
                            self.ADC_resolution,
                            self.ADC_overflow_rate,
                            self.quant_method,
                            use_bindings=self.use_bindings,
                        ).T
                    else:
                        out_ = torch.matmul(
                            unfolded_batch_input,
                            self.crossbar_operation(
                                self.crossbars,
                                lambda crossbar: crossbar.conductance_matrix,
                            ),
                        ).T
                        if self.quant_method is not None:
                            out_ = memtorch.bh.Quantize.quantize(
                                out_,
                                quant=self.ADC_resolution,
                                overflow_rate=self.ADC_overflow_rate,
                                quant_method=self.quant_method,
                            )

                    out[batch] = out_.view(size=(1, self.out_channels, *output_dim))

            out = self.transform_output(out)
            if not self.bias is None:
                out[batch] += self.bias.data.view(-1, 1, 1, 1).expand_as(out[batch])

            return out

    def tune(self, input_batch_size=4, input_shape=32):
        """Tuning method."""
        self.transform_output = naive_tune(
            self,
            (input_batch_size, self.in_channels, input_shape, input_shape, input_shape),
            self.verbose,
        )

    def __str__(self):
        return "bh.Conv3d(in_channels=%d, out_channels=%d, kernel_size=(%d, %d, %d), stride=(%d, %d, %d), padding=(%d, %d, %d))" % (
            self.in_channels,
            self.out_channels,
            self.kernel_size[0],
            self.kernel_size[1],
            self.kernel_size[2],
            self.stride[0],
            self.stride[1],
            self.stride[2],
            self.padding[0],
            self.padding[1],
            self.padding[2],
        )
