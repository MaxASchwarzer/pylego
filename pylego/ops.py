import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

LOG2PI = np.log(2.0 * np.pi)


class Identity(nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x


class ScaleGradient(Identity):

    def __init__(self, scale):
        super().__init__()
        self.scale = scale

    def backward(self, dx):
        return self.scale * dx


class View(nn.Module):

    def __init__(self, *view_as):
        super().__init__()
        self.view_as = view_as

    def forward(self, x):
        return x.view(*self.view_as)


class Upsample(nn.Module):

    def __init__(self, scale_factor=2, mode='bilinear'):
        super().__init__()
        self.scale_factor = scale_factor
        self.mode = mode

    def forward(self, x):
        return F.interpolate(x, scale_factor=self.scale_factor, mode=self.mode, align_corners=False)


class GridGaussian(nn.Module):
    '''Projects input coordinates [y, x] to a grid of size [h, w] with a 2D Gaussian of mean [y, x] and std sigma.'''

    def __init__(self, variance, h, w, hmin, hmax, wmin, wmax, mean_value=None):
        super().__init__()
        self.variance = variance
        self.h = h
        self.w = w
        if mean_value is None:
            self.mean_value = 1.0 / (2.0 * np.pi * variance)  # From pdf of Gaussian
        else:
            self.mean_value = mean_value
        ones = np.ones([h, w])
        ys_channel = np.linspace(hmin, hmax, h)[:, np.newaxis] * ones
        xs_channel = np.linspace(wmin, wmax, w)[np.newaxis, :] * ones
        initial = np.concatenate([ys_channel[np.newaxis, ...], xs_channel[np.newaxis, ...]], 0)  # 2 x h x w
        self.linear_grid = nn.Parameter(torch.Tensor(initial), requires_grad=False)

    def forward(self, loc):
        '''loc has shape [..., 2], where loc[...] = [y_i x_i].'''
        loc_grid = loc[..., None, None].expand(*loc.size(), self.h, self.w)
        expanded_lin_grid = self.linear_grid[None, ...].expand_as(loc_grid)
        # both B x 2 x h x w
        reduction_dim = len(loc_grid.size()) - 3
        return ((-(expanded_lin_grid - loc_grid).pow(2).sum(dim=reduction_dim) / (2.0 * self.variance)).exp() *
                self.mean_value)


class ResBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, rescale=None, norm=None, nonlinearity=F.elu, final=False,
                 skip_last_norm=False, layer_index=1, eps=0.0):
        super().__init__()
        self.final = final
        self.skip_last_norm = skip_last_norm
        if stride < 0:
            self.upsample = Upsample(-stride)
            stride = 1
        else:
            self.upsample = Identity()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        if norm is not None:
            self.bn1 = norm(planes, affine=True)
        else:
            self.bn1 = Identity()
        self.nonlinearity = nonlinearity
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1, bias=False)
        if norm is not None:
            self.bn2 = norm(planes, affine=True)
        else:
            self.bn2 = Identity()
        self.rescale = rescale
        self.stride = stride
        self.gain = nn.Parameter(torch.ones(1, 1, 1, 1))
        self.biases = nn.ParameterList([nn.Parameter(torch.zeros(1, 1, 1, 1)) for _ in range(4)])

        n = self.conv1.kernel_size[0] * self.conv1.kernel_size[1] * self.conv1.out_channels
        self.conv1.weight.data.normal_(0, (layer_index ** (-0.5)) *  np.sqrt(2. / n))
        if eps > 0.0:
            self.conv2.weight.data.normal_(0, eps / np.sqrt(n))
        else:
            self.conv2.weight.data.zero_()

    def forward(self, x):
        out = self.upsample(x + self.biases[0])
        out = self.conv1(out) + self.biases[1]
        out = self.bn1(out)
        out = self.nonlinearity(out) + self.biases[2]

        out = self.gain * self.conv2(out) + self.biases[3]
        if not self.final or not self.skip_last_norm:
            out = self.bn2(out)

        if self.rescale is not None:
            x = self.rescale(x)

        out += x
        if not self.final:
            out = self.nonlinearity(out)

        return out


class ResNet(nn.Module):

    def __init__(self, inplanes, layers, block=None, norm=None, nonlinearity=F.elu, skip_last_norm=False,
                 previous_blocks=0, eps=0.0):
        '''layers is a list of tuples (layer_size, input_planes, stride). Negative stride for upscaling.'''
        super().__init__()
        self.norm = norm
        self.skip_last_norm = skip_last_norm
        self.eps = eps
        if block is None:
            block = ResBlock

        self.inplanes = inplanes
        self.nonlinearity = nonlinearity
        all_layers = []
        layer_index = 1 + previous_blocks
        for i, (layer_size, inplanes, stride) in enumerate(layers):
            final = (i == len(layers) - 1)
            all_layers.append(self._make_layer(block, inplanes, layer_size, stride=stride, final=final,
                                               layer_index=layer_index))
            layer_index += layer_size
        self.layers = nn.Sequential(*all_layers)

        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, stride=1, final=False, layer_index=1):
        rescale = None
        if self.norm is not None:
            batch_norm2d = self.norm(planes * block.expansion, affine=True)
        else:
            batch_norm2d = Identity()
        if stride != 1 or self.inplanes != planes * block.expansion:
            if stride < 0:
                stride_ = -stride
                rescale = nn.Sequential(
                    Upsample(stride_),
                    nn.Conv2d(self.inplanes, planes * block.expansion, kernel_size=1, bias=False),
                    batch_norm2d,
                )
                conv = 1
            else:
                rescale = nn.Sequential(
                    nn.Conv2d(self.inplanes, planes * block.expansion, kernel_size=1, stride=stride, bias=False),
                    batch_norm2d,
                )
                conv = 0
            n = rescale[conv].kernel_size[0] * rescale[conv].kernel_size[1] * rescale[conv].out_channels
            rescale[conv].weight.data.normal_(0, np.sqrt(2. / n))

        layers = []
        layer_final = final and blocks == 1
        layers.append(block(self.inplanes, planes, stride, rescale, norm=self.norm, nonlinearity=self.nonlinearity,
                            final=layer_final, skip_last_norm=self.skip_last_norm, layer_index=layer_index,
                            eps=self.eps))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layer_final = final and i == blocks - 1
            layers.append(block(self.inplanes, planes, norm=self.norm, nonlinearity=self.nonlinearity,
                                final=layer_final, skip_last_norm=self.skip_last_norm, layer_index=layer_index+i,
                                eps=self.eps))

        return nn.Sequential(*layers)

    def forward(self, x):
        return self.layers(x)


class MultilayerLSTMCell(nn.Module):
    '''Provides a mutli-layer wrapper for LSTMCell.'''

    def __init__(self, input_size, hidden_size, bias=True, layers=1, every_layer_input=False,
                 use_previous_higher=False):
        '''
        every_layer_input: Consider raw input at every layer.
        use_previous_higher: Take higher layer at previous timestep as input to current layer.
        '''
        super().__init__()
        self.hidden_size = hidden_size
        self.layers = layers
        self.every_layer_input = every_layer_input
        self.use_previous_higher = use_previous_higher
        input_sizes = [input_size] + [hidden_size for _ in range(1, layers)]
        if every_layer_input:
            for i in range(1, layers):
                input_sizes[i] += input_size
        if use_previous_higher:
            for i in range(layers - 1):
                input_sizes[i] += hidden_size
        self.lstm_cells = nn.ModuleList([nn.LSTMCell(input_sizes[i], hidden_size, bias=bias) for i in range(layers)])

    def forward(self, input_, hx=None):
        '''
        Input: input, [(h_0, c_0), ..., (h_L, c_L)]
        Output: [(h_0, c_0), ..., (h_L, c_L)]
        '''
        if hx is None:
            hx = [None] * self.layers
        outputs = []
        recent = input_
        for layer in range(self.layers):
            if layer > 0 and self.every_layer_input:
                recent = torch.cat([recent, input_], dim=1)
            if layer < self.layers - 1 and self.use_previous_higher:
                if hx[layer + 1] is None:
                    prev = recent.new_zeros([recent.size(0), self.hidden_size])
                else:
                    prev = hx[layer + 1][0]
                recent = torch.cat([recent, prev], dim=1)
            out = self.lstm_cells[layer](recent, hx[layer])
            recent = out[0]
            outputs.append(out)
        return outputs


class MultilayerLSTM(nn.Module):
    '''A multilayer LSTM that uses MultilayerLSTMCell.'''

    def __init__(self, input_size, hidden_size, bias=True, layers=1, every_layer_input=False,
                 use_previous_higher=False):
        super().__init__()
        self.cell = MultilayerLSTMCell(input_size, hidden_size, bias=bias, layers=layers,
                                       every_layer_input=every_layer_input, use_previous_higher=use_previous_higher)

    def forward(self, input_, reset=None):
        '''If reset is 1.0, the RNN state is reset AFTER that timestep's output is produced, otherwise if reset is 0.0,
        nothing is changed.'''
        hx = None
        outputs = []
        for t in range(input_.size(1)):
            hx = self.cell(input_[:, t], hx)
            outputs.append(torch.cat([h[:, None, None, :] for (h, c) in hx], dim=2))
            if reset is not None:
                reset_t = reset[:, t, None]
                if torch.any(reset_t > 1e-6):
                    for i, (h, c) in enumerate(hx):
                        hx[i] = (h * (1.0 - reset_t), c * (1.0 - reset_t))

        return torch.cat(outputs, dim=1)  # size: batch_size, length, layers, hidden_size


def thresholded_sigmoid(x, linear_range=0.8):
    # t(x)={-l<=x<=l:0.5+x, x<-l:s(x+l)(1-2l), x>l:s(x-l)(1-2l)+2l}
    l = linear_range / 2.0
    return torch.where(x < -l, torch.sigmoid(x + l) * (1. - linear_range),
                       torch.where(x > l, torch.sigmoid(x - l) * (1. - linear_range) + linear_range, x + 0.5))


def inv_thresholded_sigmoid(x, linear_range=0.8):
    # t^-1(x)={0.5-l<=x<=0.5+l:x-0.5, x<0.5-l:-l-ln((1-2l-x)/x), x>0.5+l:l-ln((1-x)/(x-2l))}
    l = linear_range / 2.0
    return torch.where(x < 0.5 - l, -l - torch.log((1. - linear_range - x) / x),
                       torch.where(x > 0.5 + l, l - torch.log((1. - x) / (x - linear_range)), x - 0.5))


def reparameterize_gaussian(mu, logvar, sample, return_eps=False):
    std = torch.exp(0.5 * logvar)
    if sample:
        eps = torch.randn_like(std)
    else:
        eps = torch.zeros_like(std)
    ret = eps.mul(std).add_(mu)
    if return_eps:
        return ret, eps
    else:
        return ret


def kl_div_gaussian(q_mu, q_logvar, p_mu=None, p_logvar=None):
    '''Batched KL divergence D(q||p) computation.'''
    if p_mu is None or p_logvar is None:
        zero = q_mu.new_zeros(1)
        p_mu = p_mu or zero
        p_logvar = p_logvar or zero
    logvar_diff = q_logvar - p_logvar
    kl_div = -0.5 * (1.0 + logvar_diff - logvar_diff.exp() - ((q_mu - p_mu)**2 / p_logvar.exp()))
    return kl_div.sum(dim=-1)


def gaussian_log_prob(mu, logvar, x):
    '''Batched log probability log p(x) computation.'''
    logprob = -0.5 * (LOG2PI + logvar + ((x - mu)**2 / logvar.exp()))
    return logprob.sum(dim=-1)
