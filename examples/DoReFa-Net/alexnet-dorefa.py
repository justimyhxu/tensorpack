#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: alexnet-dorefa.py
# Author: Yuxin Wu, Yuheng Zou ({wyx,zyh}@megvii.com)

import cv2
import tensorflow as tf
import argparse
import numpy as np
import os
import sys


from tensorpack import *
from tensorpack.tfutils.summary import add_param_summary
from tensorpack.tfutils.sessinit import get_model_loader
from tensorpack.tfutils.varreplace import remap_variables
from tensorpack.dataflow import dataset
from tensorpack.utils.gpu import get_num_gpu

from imagenet_utils import (
    get_imagenet_dataflow, fbresnet_augmentor, ImageNetModel, eval_on_ILSVRC12)
from dorefa import get_dorefa, ternarize, get_hwgq, get_warmbin, Schdule_Relax, RelaxSetter

"""
This is a tensorpack script for the ImageNet results in paper:
DoReFa-Net: Training Low Bitwidth Convolutional Neural Networks with Low Bitwidth Gradients
http://arxiv.org/abs/1606.06160

The original experiements are performed on a proprietary framework.
This is our attempt to reproduce it on tensorpack & TensorFlow.

To Train:
    ./alexnet-dorefa.py --dorefa 1,2,6 --data PATH --gpu 0,1

    PATH should look like:
    PATH/
      train/
        n02134418/
          n02134418_198.JPEG
          ...
        ...
      val/
        ILSVRC2012_val_00000001.JPEG
        ...

    And you'll need the following to be able to fetch data efficiently
        Fast disk random access (Not necessarily SSD. I used a RAID of HDD, but not sure if plain HDD is enough)
        More than 20 CPU cores (for data processing)
        More than 10G of free memory
    On 8 P100s and dorefa==1,2,6, the training should take about 30 minutes per epoch.

To run pretrained model:
    ./alexnet-dorefa.py --load alexnet-126.npz --run a.jpg --dorefa 1,2,6
"""

BITW = 1
BITA = 2
BITG = 6
TOTAL_BATCH_SIZE = 512
BATCH_SIZE = None


class Model(ImageNetModel):
    weight_decay = 5e-6
    weight_decay_pattern = 'fc.*/W'

    def get_logits(self, image):
        if BITW == 't':
            fw, fa, fg = get_dorefa(32, 32, 32)
            fw = ternarize
        else:
            fw, fa, fg = get_dorefa(BITW, BITA, BITG)
            fa = get_warmbin(BITA)
        # monkey-patch tf.get_variable to apply fw
        def new_get_variable(v):
            name = v.op.name
            # don't binarize first and last layer
            if not name.endswith('W') or 'conv0' in name or 'fct' in name:
                return v
            else:
                logger.info("Quantizing weight {}".format(v.op.name))
                return fw(v)

        def nonlin(x):
            if BITA == 32:
                return tf.nn.relu(x)    # still use relu for 32bit cases
            return tf.clip_by_value(x, 0.0, 1.0)

        def activate(x,relax):
            #return fa(nonlin(x))
            return fa(x, relax)
        relax = tf.get_variable('relax_para', initializer=1.0, trainable=False)
        
        with remap_variables(new_get_variable), \
                argscope([Conv2D, BatchNorm, MaxPooling], data_format='channels_first'), \
                argscope(BatchNorm, momentum=0.9, epsilon=1e-4, center=False, scale=False), \
                argscope(Conv2D, use_bias=False):
            logits = (LinearWrap(image)
                      .Conv2D('conv0', 96, 12, strides=4, padding='VALID', use_bias=True)
                      .apply(activate, relax= relax)
                      .Conv2D('conv1', 256, 5, padding='SAME', split=2)
                      .apply(fg)
                      .BatchNorm('bn1')
                      .MaxPooling('pool1', 3, 2, padding='SAME')
                      .apply(activate, relax= relax)

                      .Conv2D('conv2', 384, 3)
                      .apply(fg)
                      .BatchNorm('bn2')
                      .MaxPooling('pool2', 3, 2, padding='SAME')
                      .apply(activate, relax= relax)

                      .Conv2D('conv3', 384, 3, split=2)
                      .apply(fg)
                      .BatchNorm('bn3')
                      .apply(activate, relax= relax)

                      .Conv2D('conv4', 256, 3, split=2)
                      .apply(fg)
                      .BatchNorm('bn4')
                      .MaxPooling('pool4', 3, 2, padding='VALID')
                      .apply(activate, relax= relax)

                      .FullyConnected('fc0', 4096)
                      .apply(fg)
                      .BatchNorm('bnfc0')
                      .apply(activate, relax= relax)

                      .FullyConnected('fc1', 4096, use_bias=False)
                      .apply(fg)
                      .BatchNorm('bnfc1')
                      #.apply(nonlin)
                      .tf.nn.relu()
                      .FullyConnected('fct', 1000, use_bias=True)())
        add_param_summary(('.*/W', ['histogram', 'rms']))
        tf.nn.softmax(logits, name='output')  # for prediction
        tf.summary.scalar('relax_para', relax)
        return logits

    def optimizer(self):
        lr = tf.get_variable('learning_rate', initializer=2e-4, trainable=False)
        tf.summary.scalar('lr', lr)
        return tf.train.AdamOptimizer(lr, epsilon=1e-5)


def get_data(dataset_name):
    isTrain = dataset_name == 'train'
    augmentors = fbresnet_augmentor(isTrain)
    return get_imagenet_dataflow(
        args.data, dataset_name, BATCH_SIZE, augmentors)


def get_config():
    data_train = get_data('train')
    data_test = get_data('val')

    return TrainConfig(
        dataflow=data_train,
        callbacks=[
            ModelSaver(),
            ScheduledHyperParamSetter(
                'learning_rate', [(60, 4e-5), (75, 8e-6)]),
            InferenceRunner(data_test,
                            [ClassificationError('wrong-top1', 'val-error-top1'),
                             ClassificationError('wrong-top5', 'val-error-top5')]),
            RelaxSetter(0, args.epoches*len(data_train), 1.0, 1000.0),
            MergeAllSummaries(),
        ],
        model=Model(),
        steps_per_epoch=1281167 // TOTAL_BATCH_SIZE,
        max_epoch=args.epoches,
    )


def run_image(model, sess_init, inputs):
    pred_config = PredictConfig(
        model=model,
        session_init=sess_init,
        input_names=['input'],
        output_names=['output']
    )
    predictor = OfflinePredictor(pred_config)
    meta = dataset.ILSVRCMeta()
    words = meta.get_synset_words_1000()

    transformers = imgaug.AugmentorList(fbresnet_augmentor(isTrain=False))
    for f in inputs:
        assert os.path.isfile(f), f
        img = cv2.imread(f).astype('float32')
        assert img is not None

        img = transformers.augment(img)[np.newaxis, :, :, :]
        outputs = predictor(img)[0]
        prob = outputs[0]
        ret = prob.argsort()[-10:][::-1]

        names = [words[i] for i in ret]
        print(f + ":")
        print(list(zip(names, prob[ret])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='the physical ids of GPUs to use')
    parser.add_argument('--load', help='load a checkpoint, or a npz (given as the pretrained model)')
    parser.add_argument('--data', help='ILSVRC dataset dir', default='/home/jovyan/harvard-heavy/datasets/')
    parser.add_argument('--dorefa', required=True,
                        help='number of bits for W,A,G, separated by comma. W="t" means TTQ')
    parser.add_argument('--run', help='run on a list of images with the pretrained model', nargs='*')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--root_dir', action='store', default='trash/', help='root dir for different experiments', type=str)
    parser.add_argument('--epoches', default='90', type=int)
    args = parser.parse_args()

    dorefa = args.dorefa.split(',')
    if dorefa[0] == 't':
        assert dorefa[1] == '32' and dorefa[2] == '32'
        BITW, BITA, BITG = 't', 32, 32
    else:
        BITW, BITA, BITG = map(int, dorefa)

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.run:
        assert args.load.endswith('.npz')
        run_image(Model(), DictRestore(dict(np.load(args.load))), args.run)
        sys.exit()
    if args.eval:
        BATCH_SIZE = 128
        ds = get_data('val')
        eval_on_ILSVRC12(Model(), get_model_loader(args.load), ds)
        sys.exit()

    nr_tower = max(get_num_gpu(), 1)
    BATCH_SIZE = TOTAL_BATCH_SIZE // nr_tower

    logger.set_logger_dir('../../../runs/'+args.root_dir)
    logger.info("Batch per tower: {}".format(BATCH_SIZE))

    config = get_config()
    if args.load:
        config.session_init = SaverRestore(args.load)
    launch_train_with_config(config, SyncMultiGPUTrainerReplicated(nr_tower))
