import torch.nn as nn
import torch.nn.functional as F
import torch
import torch.nn.init as init
from torch.autograd import Variable
import yaml

yamlPath = "configure.yaml"
f = open(yamlPath, 'r', encoding='utf-8')
conf = f.read()
conf_dict = yaml.safe_load(conf) 

if_cuda = True if conf_dict['if_cuda'] == '1' else False
range_x=conf_dict['range_x']
range_y=conf_dict['range_y']
range_z=conf_dict['range_z']
vox_depth = conf_dict['vox_d']
vox_width = conf_dict['vox_w']
vox_height = conf_dict['vox_h']
H = math.ceil((max(range_x)-min(range_x))/vox_height)
W = math.ceil((max(range_y)-min(range_y))/vox_width)
D = math.ceil((max(range_z)-min(range_z))/vox_depth)
       
def weights_init(m):
    if isinstance(m, nn.Conv2d):
        init.xavier_uniform_(m.weight.data)
        m.bias.data.zero_()
        
# conv2d + bn + relu
class Conv2d(nn.Module):
    def __init__(self,in_channels,out_channels,k,s,p, activation=True, batch_norm=True):
        super(Conv2d, self).__init__()
        self.conv = nn.Conv2d(in_channels,out_channels,kernel_size=k,stride=s,padding=p)
        if batch_norm:
            self.bn = nn.BatchNorm2d(out_channels)
        else:
            self.bn = None
        self.activation = activation
    def forward(self,x):
        x = self.conv(x)
        if self.bn is not None:
            x=self.bn(x)
        if self.activation:
            return F.relu(x,inplace=True)
        else:
            return x

# conv3d + bn + relu
class Conv3d(nn.Module):
    def __init__(self, in_channels, out_channels, k, s, p, batch_norm=True):
        super(Conv3d, self).__init__()
        self.conv = nn.Conv3d(in_channels, out_channels, kernel_size=k, stride=s, padding=p)
        if batch_norm:
            self.bn = nn.BatchNorm3d(out_channels)
        else:
            self.bn = None
    def forward(self, x):
        x = self.conv(x)
        if self.bn is not None:
            x = self.bn(x)
        return F.relu(x, inplace=True)

# Fully Connected Network
class FCN(nn.Module):

    def __init__(self,cin,cout):
        super(FCN, self).__init__()
        self.cout = cout
        self.linear = nn.Linear(cin, cout)
        self.bn = nn.BatchNorm1d(cout)

    def forward(self,x):
        # KK is the stacked k across batch
        kk, t, _ = x.shape
        x = self.linear(x.view(kk*t,-1))
        x = F.relu(self.bn(x))
        return x.view(kk,t,-1)

# Voxel Feature Encoding layer
class VFE(nn.Module):

    def __init__(self,cin,cout):
        super(VFE, self).__init__()
        assert cout % 2 == 0
        self.units = cout // 2
        self.fcn = FCN(cin,self.units)

    def forward(self, x, mask):
        # point-wise feauture
        pwf = self.fcn(x)
        #locally aggregated feature
        laf = torch.max(pwf,1)[0].unsqueeze(1).repeat(1,conf_dict['pt_thres_per_vox'],1)
        # point-wise concat feature
        pwcf = torch.cat((pwf,laf),dim=2)
        # apply mask
        mask = mask.unsqueeze(2).repeat(1, 1, self.units * 2)
        pwcf = pwcf * mask.float()

        return pwcf

# Stacked Voxel Feature Encoding
class SVFE(nn.Module):

    def __init__(self):
        super(SVFE, self).__init__()
        self.vfe_1 = VFE(7,32)
        self.vfe_2 = VFE(32,128)
        self.fcn = FCN(128,128)
    def forward(self, x):
        mask = torch.ne(torch.max(x,2)[0], 0)
        x = self.vfe_1(x, mask)
        x = self.vfe_2(x, mask)
        x = self.fcn(x)
        # element-wise max pooling
        x = torch.max(x,1)[0]
        return x

# Convolutional Middle Layer
class ConvoMidLayer(nn.Module):
    def __init__(self):
        super(ConvoMidLayer, self).__init__()
        self.conv3d_1 = Conv3d(128, 64, 3, s=(2, 1, 1), p=(1, 1, 1))
        self.conv3d_2 = Conv3d(64, 64, 3, s=(1, 1, 1), p=(0, 1, 1))
        self.conv3d_3 = Conv3d(64, 64, 3, s=(2, 1, 1), p=(1, 1, 1))

    def forward(self, x):
        x = self.conv3d_1(x)
        x = self.conv3d_2(x)
        x = self.conv3d_3(x)
        return x

# Region Proposal Network
class RPN(nn.Module):
    def __init__(self):
        super(RPN, self).__init__()
        self.block_1 = [Conv2d(128, 128, 3, 2, 1)]
        self.block_1 += [Conv2d(128, 128, 3, 1, 1) for _ in range(3)]
        self.block_1 = nn.Sequential(*self.block_1)

        self.block_2 = [Conv2d(128, 128, 3, 2, 1)]
        self.block_2 += [Conv2d(128, 128, 3, 1, 1) for _ in range(5)]
        self.block_2 = nn.Sequential(*self.block_2)

        self.block_3 = [Conv2d(128, 256, 3, 2, 1)]
        self.block_3 += [nn.Conv2d(256, 256, 3, 1, 1) for _ in range(5)]
        self.block_3 = nn.Sequential(*self.block_3)

        self.deconv_1 = nn.Sequential(nn.ConvTranspose2d(256, 256, 4, 4, 0),nn.BatchNorm2d(256))
        self.deconv_2 = nn.Sequential(nn.ConvTranspose2d(128, 256, 2, 2, 0),nn.BatchNorm2d(256))
        self.deconv_3 = nn.Sequential(nn.ConvTranspose2d(128, 256, 1, 1, 0),nn.BatchNorm2d(256))

        self.score_head = Conv2d(768, conf_dict['anchors_per_vox'], 1, 1, 0, activation=False, batch_norm=False)
        self.reg_head = Conv2d(768, 7 * conf_dict['anchors_per_vox'], 1, 1, 0, activation=False, batch_norm=False)

    def forward(self,x):
        x = self.block_1(x)
        x_skip_1 = x
        x = self.block_2(x)
        x_skip_2 = x
        x = self.block_3(x)
        x_0 = self.deconv_1(x)
        x_1 = self.deconv_2(x_skip_2)
        x_2 = self.deconv_3(x_skip_1)
        x = torch.cat((x_0,x_1,x_2),1)
        return self.score_head(x),self.reg_head(x)

class VoxelNet(nn.Module):
    def __init__(self):
        super(VoxelNet, self).__init__()
        self.svfe = SVFE()
        self.cml = ConvoMidLayer()
        self.rpn = RPN()
        
    def voxel_indexing(self, sparse_features, coords):
        dim = sparse_features.shape[-1]
        if if_cuda:
            dense_feature = Variable(torch.zeros(dim, conf_dict['batch_size'], D, H, W).cuda())
        else:
            dense_feature = Variable(torch.zeros(dim, conf_dict['batch_size'], D, H, W))
        dense_feature[:, coords[:,0], coords[:,1], coords[:,2], coords[:,3]]= sparse_features.transpose(0,1)
        return dense_feature.transpose(0,1)

    def forward(self, voxel_features, voxel_coords):
        # feature learning network
        vwfs = self.svfe(voxel_features)
        vwfs = self.voxel_indexing(vwfs,voxel_coords)
        # convolutional middle network
        cml_out = self.cml(vwfs)
        # region proposal network
        # merge the depth and feature dim into one, output probability score map and regression map
        psm,rm = self.rpn(cml_out.view(conf_dict['batch_size'], -1, H, W))
        return psm, rm