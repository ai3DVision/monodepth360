# Modifications Srijan Parmeshwar 2017.
# Copybottom UCL Business plc 2017. Patent Pending. All bottoms reserved.
#
# The MonoDepth Software is licensed under the terms of the UCLB ACP-A licence
# which allows for non-commercial use only, the full terms of which are made
# available in the LICENSE file.
#
# For any other use of the software not covered by the UCLB ACP-A Licence, 
# please contact info@uclb.com

"""Fully convolutional model for monocular depth estimation
    by Clement Godard, Oisin Mac Aodha and Gabriel J. Brostow
    http://visual.cs.ucl.ac.uk/pubs/monoDepth/
"""

from collections import namedtuple

import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim

from bilinear_sampler import *

monodepth_parameters = namedtuple('parameters',
                        'height, width, '
                        'batch_size, '
                        'num_threads, '
                        'num_epochs, '
                        'wrap_mode, '
                        'use_deconv, '
                        'alpha_image_loss, '
                        'disparity_gradient_loss_weight, '
                        'tb_loss_weight, '
                        'full_summary')

class MonodepthModel(object):
    """Monodepth model"""

    def __init__(self, params, mode, top, bottom, reuse_variables=None, model_index=0):
        self.params = params
        self.mode = mode
        self.top = top
        self.bottom = bottom
        self.model_collection = ['model_' + str(model_index)]

        self.reuse_variables = reuse_variables

        self.build_model()
        self.build_outputs()

        if self.mode == 'test':
            return

        self.build_losses()
        self.build_summaries()     

    def gradient_x(self, img):
        gx = img[:,:,:-1,:] - img[:,:,1:,:]
        return gx

    def gradient_y(self, img):
        gy = img[:,:-1,:,:] - img[:,1:,:,:]
        return gy

    def upsample_nn(self, x, ratio):
        s = tf.shape(x)
        h = s[1]
        w = s[2]
        return tf.image.resize_nearest_neighbor(x, [h * ratio, w * ratio])

    def scale_pyramid(self, img, num_scales):
        scaled_imgs = [img]
        s = tf.shape(img)
        h = s[1]
        w = s[2]
        for i in range(num_scales - 1):
            ratio = 2 ** (i + 1)
            nh = h / ratio
            nw = w / ratio
            scaled_imgs.append(tf.image.resize_area(img, [nh, nw]))
        return scaled_imgs

    def lat_long_grid(self, width, height, batch_size):
        S, T = tf.meshgrid(tf.linspace(- np.pi, np.pi, width), tf.linspace(- np.pi / 2, np.pi / 2, height))
        S_generator = tf.expand_dims(S, 0)
        T_generator = tf.expand_dims(T, 0)
        S_grids = tf.expand_dims(tf.tile(S_generator, [batch_size, 1, 1]), 3)
        T_grids = tf.expand_dims(tf.tile(T_generator, [batch_size, 1, 1]), 3)
        return S_grids, T_grids

    def depth_to_disparity(self, depth):
        baseline_distance = 0.5
        _, T_grids = self.lat_long_grid(tf.shape(depth)[2], tf.shape(depth)[1], tf.shape(depth)[0])
        return tf.atan2(baseline_distance * depth, (1.0 + tf.tan(T_grids) ** 2.0) * (depth ** 2.0) - baseline_distance * depth * tf.tan(T_grids))

    def generate_image_top(self, img, disp):
        return transformer(img, -disp, 'vertical')

    def generate_image_bottom(self, img, disp):
        return transformer(img, disp, 'vertical')

    def SSIM(self, x, y):
        C1 = 0.01 ** 2
        C2 = 0.03 ** 2

        mu_x = slim.avg_pool2d(x, 3, 1, 'VALID')
        mu_y = slim.avg_pool2d(y, 3, 1, 'VALID')

        sigma_x  = slim.avg_pool2d(x ** 2, 3, 1, 'VALID') - mu_x ** 2
        sigma_y  = slim.avg_pool2d(y ** 2, 3, 1, 'VALID') - mu_y ** 2
        sigma_xy = slim.avg_pool2d(x * y , 3, 1, 'VALID') - mu_x * mu_y

        SSIM_n = (2 * mu_x * mu_y + C1) * (2 * sigma_xy + C2)
        SSIM_d = (mu_x ** 2 + mu_y ** 2 + C1) * (sigma_x + sigma_y + C2)

        SSIM = SSIM_n / SSIM_d

        return tf.clip_by_value((1 - SSIM) / 2, 0, 1)

    def get_depth_smoothness(self, depth, pyramid):
        depth_gradients_x = [self.gradient_x(d) for d in depth]
        depth_gradients_y = [self.gradient_y(d) for d in depth]

        image_gradients_x = [self.gradient_x(img) for img in pyramid]
        image_gradients_y = [self.gradient_y(img) for img in pyramid]

        weights_x = [tf.exp(-tf.reduce_mean(tf.abs(g), 3, keep_dims=True)) for g in image_gradients_x]
        weights_y = [tf.exp(-tf.reduce_mean(tf.abs(g), 3, keep_dims=True)) for g in image_gradients_y]

        smoothness_x = [depth_gradients_x[i] * weights_x[i] for i in range(4)]
        smoothness_y = [depth_gradients_y[i] * weights_y[i] for i in range(4)]
        return smoothness_x + smoothness_y

    def get_depth(self, x):
        depth = self.conv(x, 2, 3, 1, tf.nn.elu)
        return depth

    def conv(self, x, num_out_layers, kernel_size, stride, activation_fn=tf.nn.elu):
        p = np.floor((kernel_size - 1) / 2).astype(np.int32)
        p_x = tf.pad(x, [[0, 0], [p, p], [p, p], [0, 0]])
        return slim.conv2d(p_x, num_out_layers, kernel_size, stride, 'VALID', activation_fn=activation_fn)

    def conv_block(self, x, num_out_layers, kernel_size):
        conv1 = self.conv(x,     num_out_layers, kernel_size, 1)
        conv2 = self.conv(conv1, num_out_layers, kernel_size, 2)
        return conv2

    def maxpool(self, x, kernel_size):
        p = np.floor((kernel_size - 1) / 2).astype(np.int32)
        p_x = tf.pad(x, [[0, 0], [p, p], [p, p], [0, 0]])
        return slim.max_pool2d(p_x, kernel_size)

    def resconv(self, x, num_layers, stride):
        do_proj = tf.shape(x)[3] != num_layers or stride == 2
        shortcut = []
        conv1 = self.conv(x,         num_layers, 1, 1)
        conv2 = self.conv(conv1,     num_layers, 3, stride)
        conv3 = self.conv(conv2, 4 * num_layers, 1, 1, None)
        if do_proj:
            shortcut = self.conv(x, 4 * num_layers, 1, stride, None)
        else:
            shortcut = x
        return tf.nn.elu(conv3 + shortcut)

    def resblock(self, x, num_layers, num_blocks):
        out = x
        for i in range(num_blocks - 1):
            out = self.resconv(out, num_layers, 1)
        out = self.resconv(out, num_layers, 2)
        return out

    def upconv(self, x, num_out_layers, kernel_size, scale):
        upsample = self.upsample_nn(x, scale)
        conv = self.conv(upsample, num_out_layers, kernel_size, 1)
        return conv

    def deconv(self, x, num_out_layers, kernel_size, scale):
        p_x = tf.pad(x, [[0, 0], [1, 1], [1, 1], [0, 0]])
        conv = slim.conv2d_transpose(p_x, num_out_layers, kernel_size, scale, 'SAME')
        return conv[:,3:-1,3:-1,:]

    def build_resnet50(self):
        #set convenience functions
        conv   = self.conv
        if self.params.use_deconv:
            upconv = self.deconv
        else:
            upconv = self.upconv

        with tf.variable_scope('encoder'):
            conv1 = conv(self.model_input, 64, 7, 2) # H/2  -   64D
            pool1 = self.maxpool(conv1,           3) # H/4  -   64D
            conv2 = self.resblock(pool1,      64, 3) # H/8  -  256D
            conv3 = self.resblock(conv2,     128, 4) # H/16 -  512D
            conv4 = self.resblock(conv3,     256, 6) # H/32 - 1024D
            conv5 = self.resblock(conv4,     512, 3) # H/64 - 2048D

        with tf.variable_scope('skips'):
            skip1 = conv1
            skip2 = pool1
            skip3 = conv2
            skip4 = conv3
            skip5 = conv4
        
        # DECODING
        with tf.variable_scope('decoder'):
            upconv6 = upconv(conv5,   512, 3, 2) #H/32
            concat6 = tf.concat([upconv6, skip5], 3)
            iconv6  = conv(concat6,   512, 3, 1)

            upconv5 = upconv(iconv6, 256, 3, 2) #H/16
            concat5 = tf.concat([upconv5, skip4], 3)
            iconv5  = conv(concat5,   256, 3, 1)

            upconv4 = upconv(iconv5,  128, 3, 2) #H/8
            concat4 = tf.concat([upconv4, skip3], 3)
            iconv4  = conv(concat4,   128, 3, 1)
            self.depth4 = self.get_depth(iconv4)
            udepth4  = self.upsample_nn(self.depth4, 2)

            upconv3 = upconv(iconv4,   64, 3, 2) #H/4
            concat3 = tf.concat([upconv3, skip2, udepth4], 3)
            iconv3  = conv(concat3,    64, 3, 1)
            self.depth3 = self.get_depth(iconv3)
            udepth3  = self.upsample_nn(self.depth3, 2)

            upconv2 = upconv(iconv3,   32, 3, 2) #H/2
            concat2 = tf.concat([upconv2, skip1, udepth3], 3)
            iconv2  = conv(concat2,    32, 3, 1)
            self.depth2 = self.get_depth(iconv2)
            udepth2  = self.upsample_nn(self.depth2, 2)

            upconv1 = upconv(iconv2,  16, 3, 2) #H
            concat1 = tf.concat([upconv1, udepth2], 3)
            iconv1  = conv(concat1,   16, 3, 1)
            self.depth1 = self.get_depth(iconv1)

    def build_model(self):
        with slim.arg_scope([slim.conv2d, slim.conv2d_transpose], activation_fn=tf.nn.elu):
            with tf.variable_scope('model', reuse=self.reuse_variables):

                self.top_pyramid = self.scale_pyramid(self.top, 4)

                if self.mode == 'train':
                    self.bottom_pyramid = self.scale_pyramid(self.bottom, 4)

                self.model_input = self.top

                #build model
                self.build_resnet50()

    def build_outputs(self):
        # STORE DEPTHS
        with tf.variable_scope('depths'):
            self.depth_est  = [self.depth1, self.depth2, self.depth3, self.depth4]
            self.depth_top_est  = [tf.expand_dims(depth[:,:,:,0], 3) for depth in self.depth_est]
            self.depth_bottom_est = [tf.expand_dims(depth[:,:,:,1], 3) for depth in self.depth_est]

        with tf.variable_scope('disparities'):
            self.disparity_top_est = [self.depth_to_disparity(depth) for depth in self.depth_top_est]
            self.disparity_bottom_est = [self.depth_to_disparity(depth) for depth in self.depth_bottom_est]

        # GENERATE IMAGES
        with tf.variable_scope('images'):
            self.top_est  = [self.generate_image_top(self.bottom_pyramid[i], self.disparity_top_est[i])  for i in range(4)]
            self.bottom_est = [self.generate_image_bottom(self.top_pyramid[i], self.disparity_bottom_est[i]) for i in range(4)]

        if self.mode == 'test':
            return

        # TB CONSISTENCY
        with tf.variable_scope('top-bottom'):
            self.bottom_to_top_depth = [self.generate_image_top(self.depth_bottom_est[i], self.disparity_top_est[i])  for i in range(4)]
            self.top_to_bottom_depth = [self.generate_image_bottom(self.depth_top_est[i], self.disparity_bottom_est[i]) for i in range(4)]

        # DEPTH SMOOTHNESS
        with tf.variable_scope('smoothness'):
            self.depth_top_smoothness  = self.get_depth_smoothness(self.depth_top_est,  self.top_pyramid)
            self.depth_bottom_smoothness = self.get_depth_smoothness(self.depth_bottom_est, self.bottom_pyramid)

    def build_losses(self):
        with tf.variable_scope('losses', reuse=self.reuse_variables):
            # IMAGE RECONSTRUCTION
            # L1
            self.l1_top = [tf.abs( self.top_est[i] - self.top_pyramid[i]) for i in range(4)]
            self.l1_reconstruction_loss_top  = [tf.reduce_mean(l) for l in self.l1_top]
            self.l1_bottom = [tf.abs(self.bottom_est[i] - self.bottom_pyramid[i]) for i in range(4)]
            self.l1_reconstruction_loss_bottom = [tf.reduce_mean(l) for l in self.l1_bottom]

            # SSIM
            self.ssim_top = [self.SSIM( self.top_est[i],  self.top_pyramid[i]) for i in range(4)]
            self.ssim_loss_top  = [tf.reduce_mean(s) for s in self.ssim_top]
            self.ssim_bottom = [self.SSIM(self.bottom_est[i], self.bottom_pyramid[i]) for i in range(4)]
            self.ssim_loss_bottom = [tf.reduce_mean(s) for s in self.ssim_bottom]

            # WEIGTHED SUM
            self.image_loss_bottom = [self.params.alpha_image_loss * self.ssim_loss_bottom[i] + (1 - self.params.alpha_image_loss) * self.l1_reconstruction_loss_bottom[i] for i in range(4)]
            self.image_loss_top  = [self.params.alpha_image_loss * self.ssim_loss_top[i]  + (1 - self.params.alpha_image_loss) * self.l1_reconstruction_loss_top[i]  for i in range(4)]
            self.image_loss = tf.add_n(self.image_loss_top + self.image_loss_bottom)

            # DEPTH SMOOTHNESS
            self.depth_top_loss  = [tf.reduce_mean(tf.abs(self.depth_top_smoothness[i]))  / 2 ** i for i in range(4)]
            self.depth_bottom_loss = [tf.reduce_mean(tf.abs(self.depth_bottom_smoothness[i])) / 2 ** i for i in range(4)]
            self.depth_gradient_loss = tf.add_n(self.depth_top_loss + self.depth_bottom_loss)

            # TB CONSISTENCY
            self.tb_top_loss  = [tf.reduce_mean(tf.abs(self.bottom_to_top_depth[i] - self.depth_top_est[i]))  for i in range(4)]
            self.tb_bottom_loss = [tf.reduce_mean(tf.abs(self.top_to_bottom_depth[i] - self.depth_bottom_est[i])) for i in range(4)]
            self.tb_loss = tf.add_n(self.tb_top_loss + self.tb_bottom_loss)

            # TOTAL LOSS
            self.total_loss = self.image_loss + self.params.depth_gradient_loss_weight * self.depth_gradient_loss + self.params.tb_loss_weight * self.tb_loss

    def build_summaries(self):
        # SUMMARIES
        with tf.device('/cpu:0'):
            for i in range(4):
                tf.summary.scalar('ssim_loss_' + str(i), self.ssim_loss_top[i] + self.ssim_loss_bottom[i], collections=self.model_collection)
                tf.summary.scalar('l1_loss_' + str(i), self.l1_reconstruction_loss_top[i] + self.l1_reconstruction_loss_bottom[i], collections=self.model_collection)
                tf.summary.scalar('image_loss_' + str(i), self.image_loss_top[i] + self.image_loss_bottom[i], collections=self.model_collection)
                tf.summary.scalar('depth_gradient_loss_' + str(i), self.depth_top_loss[i] + self.depth_bottom_loss[i], collections=self.model_collection)
                tf.summary.scalar('tb_loss_' + str(i), self.tb_top_loss[i] + self.tb_bottom_loss[i], collections=self.model_collection)
                tf.summary.image('depth_top_est_' + str(i), self.depth_top_est[i], max_outputs=4, collections=self.model_collection)
                tf.summary.image('depth_bottom_est_' + str(i), self.depth_bottom_est[i], max_outputs=4, collections=self.model_collection)

                if self.params.full_summary:
                    tf.summary.image('top_est_' + str(i), self.top_est[i], max_outputs=4, collections=self.model_collection)
                    tf.summary.image('bottom_est_' + str(i), self.bottom_est[i], max_outputs=4, collections=self.model_collection)
                    tf.summary.image('ssim_top_'  + str(i), self.ssim_top[i],  max_outputs=4, collections=self.model_collection)
                    tf.summary.image('ssim_bottom_' + str(i), self.ssim_bottom[i], max_outputs=4, collections=self.model_collection)
                    tf.summary.image('l1_top_'  + str(i), self.l1_top[i],  max_outputs=4, collections=self.model_collection)
                    tf.summary.image('l1_bottom_' + str(i), self.l1_bottom[i], max_outputs=4, collections=self.model_collection)

            if self.params.full_summary:
                tf.summary.image('top',  self.top,   max_outputs=4, collections=self.model_collection)
                tf.summary.image('bottom', self.bottom,  max_outputs=4, collections=self.model_collection)

