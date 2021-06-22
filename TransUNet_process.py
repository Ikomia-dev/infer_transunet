# Copyright (C) 2021 Ikomia SAS
# Contact: https://www.ikomia.com
#
# This file is part of the IkomiaStudio software.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ikomia import core, dataprocess
import copy
import random
import torch
import yaml
from networks.vit_seg_modeling import VisionTransformer as ViT_seg
from ml_collections import ConfigDict
import numpy as np
from torchvision import transforms
import cv2
import os
from torchvision.transforms import InterpolationMode

# Your imports below

# --------------------
# - Class to handle the process parameters
# - Inherits PyCore.CProtocolTaskParam from Ikomia API
# --------------------
class TransUNetParam(core.CProtocolTaskParam):

    def __init__(self):
        core.CProtocolTaskParam.__init__(self)
        # Place default value initialization here
        self.configFile = ""
        self.modelFile = ""

    def setParamMap(self, paramMap):
        # Set parameters values from Ikomia application
        # Parameters values are stored as string and accessible like a python dict
        self.configFile = paramMap["configFile"]
        self.modelFile = paramMap["modelFile"]
        pass

    def getParamMap(self):
        # Send parameters values to Ikomia application
        # Create the specific dict structure (string container)
        paramMap = core.ParamMap()
        paramMap["configFile"] = self.configFile
        paramMap["modelFile"] = self.modelFile
        return paramMap


# --------------------
# - Class which implements the process
# - Inherits PyCore.CProtocolTask or derived from Ikomia API
# --------------------
class TransUNetProcess(dataprocess.CImageProcess2d):

    def __init__(self, name, param):
        dataprocess.CImageProcess2d.__init__(self, name)

        # add output + set data type
        self.setOutputDataType(core.IODataType.IMAGE_LABEL, 0)
        self.addOutput(dataprocess.CImageProcessIO(core.IODataType.IMAGE))
        self.addOutput(dataprocess.CImageProcessIO(core.IODataType.IMAGE))
        self.model = None
        self.cfg = None
        self.colors = None
        self.update = False
        self.classes = None

        # Create parameters class
        if param is None:
            self.setParam(TransUNetParam())
        else:
            self.setParam(copy.deepcopy(param))

    def getProgressSteps(self, eltCount=1):
        # Function returning the number of progress steps for this process
        # This is handled by the main progress bar of Ikomia application
        return 1

    def run(self):
        # Core function of your process
        # Call beginTaskRun for initialization
        self.beginTaskRun()

        # we use seed to keep the same color for our masks + boxes + labels (same random each time)
        random.seed(10)
        # Get input :
        input = self.getInput(0)
        srcImage = input.getImage()

        # Get output :
        mask_output = self.getOutput(0)
        graph_output = self.getOutput(2)

        # Get parameters :
        param = self.getParam()

        # Config file and model file needed are in the output folder generated by the train plugin
        if (self.cfg is None or param.update) and param.configFile != "":
            with open(param.configFile, 'r') as file:
                str = yaml.load(file, Loader=yaml.Loader)
                self.cfg = ConfigDict(str)
                self.classes = self.cfg.class_names

        if (self.model is None or param.update) and self.cfg is not None:
            print("Building model...")
            self.model = ViT_seg(self.cfg, img_size=self.cfg.img_size, num_classes=self.cfg.n_classes)
            print("Model built")
            if torch.cuda.is_available():
                self.model.cuda()

            if os.path.isfile(param.modelFile):
                print("Loading weights...")
                self.model.load_state_dict(torch.load(param.modelFile))
                print("Weights loaded")
            self.model.eval()

        if self.model is not None and srcImage is not None:
            h, w, c = np.shape(srcImage)

            downsample_img = transforms.Resize(size=(self.cfg.img_size, self.cfg.img_size),interpolation=InterpolationMode.BICUBIC)
            upsample_pred = transforms.Resize(size=(h,w), interpolation=InterpolationMode.NEAREST)

            with torch.no_grad():
                srcImage = torch.tensor([srcImage]).permute(0,3,1,2).float()
                if torch.cuda.is_available():
                    srcImage=srcImage.cuda()
                srcImage = downsample_img(srcImage)

                if self.cfg.pretrained_path is not None:
                    mean = np.array([123.675, 116.280, 103.530], dtype=np.float)
                    std = np.array([58.395, 57.120, 57.375], dtype=np.float)
                    srcImage = Normalize(mean=mean,std=std)(srcImage)

                pred = self.model(srcImage)

                pred = torch.argmax(torch.softmax(pred, dim=1), dim=1, keepdim=True)
                pred = upsample_pred(pred)
                pred= pred.squeeze()
                pred = pred.cpu().numpy()

            # Convert logits to labelled image
            dstImage = pred
            # Set image of input/output (numpy array):
            # dstImage +1 because value 0 is for background but no background here
            mask_output.setImage(dstImage)

            # Create random color map
            if self.colors == None or param.update:
                n = len(self.classes)
                self.colors = [[0, 0, 0]]
                for i in range(n - 1):
                    self.colors.append([random.randint(0, 255), random.randint(0, 255), random.randint(0, 255), 255])

                # Apply color map on labelled image
                self.setOutputColorMap(1, 0, self.colors)
            self.forwardInputImage(0, 1)

            graph_output.setImage(self.draw_legend())
            param.update = False
        # Step progress bar:
        self.emitStepProgress()

        # Call endTaskRun to finalize process
        self.endTaskRun()

    def draw_legend(self):
        img_h = 1000
        img_w = 1000
        max_height = 100
        rectangle_height = min(max_height,img_h // len(self.colors))
        rectangle_width = img_w // 3
        offset_x = 10
        offset_y = 5
        interline = 5
        legend = np.full((img_h, img_w, 3), dtype=np.int, fill_value=255)
        font = cv2.FONT_HERSHEY_SIMPLEX
        fontscale = 1
        thickness = 2
        for i, c in enumerate(self.colors):
            legend = cv2.rectangle(legend, (offset_x, i * rectangle_height + offset_y + interline),
                                   (offset_x + rectangle_width, (i + 1) * rectangle_height + offset_y - interline),
                                   color=c, thickness=-1)
            legend = cv2.putText(legend,self.classes[i],(3*offset_x+rectangle_width,(i+1)*rectangle_height+
                                                           offset_y-interline - rectangle_height//3),
                                  font, fontscale, color=[0,0,0], thickness=thickness)
        return legend

class Normalize(object):
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def __call__(self, image):
        for t, m, s in zip(image, self.mean, self.std):
            t.sub_(m).div_(s)

        return image

# --------------------
# - Factory class to build process object
# - Inherits PyDataProcess.CProcessFactory from Ikomia API
# --------------------
class TransUNetProcessFactory(dataprocess.CProcessFactory):

    def __init__(self):
        dataprocess.CProcessFactory.__init__(self)
        # Set process information as string here
        self.info.name = "TransUNet"
        self.info.shortDescription = "TransUNet inference for semantic segmentation"
        self.info.description = "This Ikomia plugin can make inference of pre-trained model from " \
                                "a given config file and a weight file produced by the Ikomia " \
                                "plugin TransUNet_Train."
        self.info.authors = "Jieneng Chen, Yongyi Lu, Qihang Yu, Xiangde Luo, Ehsan Adeli, Yan Wang, Le Lu, " \
                            "Alan L. Yuille, Yuyin Zhou"
        # relative path -> as displayed in Ikomia application process tree
        self.info.path = "Plugins/Python"
        self.info.version = "1.0.0"
        # self.info.iconPath = "your path to a specific icon"
        self.info.iconPath = "icons/transunet.png"
        self.info.article = "TransUNet: Transformers Make Strong Encoders for Medical Image Segmentation"
        self.info.journal = "not published yet"
        self.info.year = 2021
        self.info.license = "Apache-2.0 License"
        # URL of documentation
        self.info.documentationLink = "https://arxiv.org/abs/2102.04306"
        # Code source repository
        self.info.repository = "https://github.com/Beckschen/TransUNet"
        # Keywords used for search
        self.info.keywords = "semantic, segmentation, encoder, decoder, Transformers, U-Net "

    def create(self, param=None):
        # Create process object
        return TransUNetProcess(self.info.name, param)
