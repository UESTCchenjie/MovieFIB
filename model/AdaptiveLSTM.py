#!/usr/bin/Python
# -*- coding: utf-8 -*-

from lasagne.layers import Layer, MergeLayer
from lasagne.layers import Gate
import theano
import theano.tensor as T
from lasagne import nonlinearities, init
from lasagne.utils import unroll_scan
import numpy as np


class AttentionGate(object):
    def __init__(self, W_g=init.Normal(0.1), W_s=init.Normal(0.1),
                 W_h=init.Normal(0.1), W_v=init.Normal(0.1),
                 nonlinearity=nonlinearities.softmax):
        self.W_s = W_s
        self.W_h = W_h
        self.W_g = W_g
        self.W_v = W_v
        if nonlinearity is None:
            self.nonlinearity = nonlinearities.identity
        else:
            self.nonlinearity = nonlinearity


class AdaptiveLSTMLayer(MergeLayer):
    def __init__(self, incoming, num_units, num_dims,
                 ingate=Gate(),
                 forgetgate=Gate(),
                 cell=Gate(W_cell=None, nonlinearity=nonlinearities.tanh),
                 outgate=Gate(),
                 ggate=Gate(W_cell=None),
                 attenGate=AttentionGate(),
                 nonlinearity=nonlinearities.tanh,
                 cell_init=init.Constant(0.),
                 hid_init=init.Constant(0.),
                 It_init=init.Constant(0.),
                 W_p=init.Normal(0.1),
                 backwards=False,
                 learn_init=False,
                 peepholes=True,
                 gradient_steps=-1,
                 grad_clipping=0,
                 unroll_scan=False,
                 precompute_input=True,
                 mask_input=None,
                 visual_input=None,
                 **kwargs):
        # ggate ===> gt
        incomings = [incoming]
        self.mask_incoming_index = -1
        self.hid_init_incoming_index = -1
        self.cell_init_incoming_index = -1
        self.visual_input_index = -1
        if mask_input is not None:
            incomings.append(mask_input)
            self.mask_incoming_index = len(incomings)-1
        if isinstance(hid_init, Layer):
            incomings.append(hid_init)
            self.hid_init_incoming_index = len(incomings)-1
        if isinstance(cell_init, Layer):
            incomings.append(cell_init)
            self.cell_init_incoming_index = len(incomings)-1
        if isinstance(visual_input, Layer):
            incomings.append(visual_input)
            self.visual_input_index = len(incomings)-1
        # Initialize parent layer
        super(AdaptiveLSTMLayer, self).__init__(incomings, **kwargs)

        # If the provided nonlinearity is None, make it linear
        if nonlinearity is None:
            self.nonlinearity = nonlinearities.identity
        else:
            self.nonlinearity = nonlinearity

        self.learn_init = learn_init
        self.num_units = num_units
        self.backwards = backwards
        self.peepholes = peepholes
        self.gradient_steps = gradient_steps
        self.grad_clipping = grad_clipping
        self.unroll_scan = unroll_scan
        self.precompute_input = precompute_input
        self.num_dims = num_dims

        if unroll_scan and gradient_steps != -1:
            raise ValueError(
                "Gradient steps must be -1 when unroll_scan is true.")

        # Retrieve the dimensionality of the incoming layer
        input_shape = self.input_shapes[0]

        if unroll_scan and input_shape[1] is None:
            raise ValueError("Input sequence length cannot be specified as "
                             "None when unroll_scan is True")

        # num_inputs 是d
        num_inputs = np.prod(input_shape[2:])

        # self.seq_len 是 k, 就是序列长度
        self.video_len = self.input_shapes[self.visual_input_index][1]
        self.batch_size = input_shape[0]

        def add_gate_params(gate, gate_name):
            """ Convenience function for adding layer parameters from a Gate
            instance. """
            return (self.add_param(gate.W_in, (num_inputs, num_units),
                                   name="W_in_to_{}".format(gate_name)),
                    self.add_param(gate.W_hid, (num_units, num_units),
                                   name="W_hid_to_{}".format(gate_name)),
                    self.add_param(gate.b, (num_units,),
                                   name="b_{}".format(gate_name),
                                   regularizable=False),
                    gate.nonlinearity)

        def add_attenGate_params(atten_gate, atten_gate_name):
            return (self.add_param(atten_gate.W_s, (num_units, self.video_len),
                                   name="W_h_to_{}".format(atten_gate_name)),
                    self.add_param(atten_gate.W_h, (self.video_len, ),
                                   name="W_h_to_{}".format(atten_gate_name)),
                    self.add_param(atten_gate.W_g, (num_units, self.video_len),
                                   name="W_g_to_{}".format(atten_gate_name)),
                    self.add_param(atten_gate.W_v, (num_units, self.video_len),
                                   name="W_v_to_{}".format(atten_gate_name)),
                    atten_gate.nonlinearity)

        # Add in parameters from the supplied Gate instances
        (self.W_in_to_ingate, self.W_hid_to_ingate, self.b_ingate,
         self.nonlinearity_ingate) = add_gate_params(ingate, 'ingate')

        (self.W_in_to_forgetgate, self.W_hid_to_forgetgate, self.b_forgetgate,
         self.nonlinearity_forgetgate) = add_gate_params(forgetgate,
                                                         'forgetgate')

        (self.W_in_to_cell, self.W_hid_to_cell, self.b_cell,
         self.nonlinearity_cell) = add_gate_params(cell, 'cell')

        (self.W_in_to_outgate, self.W_hid_to_outgate, self.b_outgate,
         self.nonlinearity_outgate) = add_gate_params(outgate, 'outgate')

        (self.W_in_to_ggate, self.W_hid_to_ggate, self.b_ggate,
         self.nonlinearity_ggate) = add_gate_params(ggate, 'ggate')

        (self.W_s_to_attenGate, self.W_h_to_attenGate, self.W_g_to_attenGate,
         self.W_v_to_attenGate, self.nonlinearity_attenGate) = add_attenGate_params(attenGate, 'attenGate')

        self.W_p = self.add_param(
             W_p,
             (self.num_units, self.num_dims),
             name='W_p'
         )

        # If peephole (cell to gate) connections were enabled, initialize
        # peephole connections.  These are elementwise products with the cell
        # state, so they are represented as vectors.
        if self.peepholes:
            self.W_cell_to_ingate = self.add_param(
                ingate.W_cell, (num_units, ), name="W_cell_to_ingate")

            self.W_cell_to_forgetgate = self.add_param(
                forgetgate.W_cell, (num_units, ), name="W_cell_to_forgetgate")

            self.W_cell_to_outgate = self.add_param(
                outgate.W_cell, (num_units, ), name="W_cell_to_outgate")

        # Setup initial values for the cell and the hidden units
        if isinstance(cell_init, Layer):
            self.cell_init = cell_init
        else:
            self.cell_init = self.add_param(
                cell_init, (1, num_units), name="cell_init",
                trainable=learn_init, regularizable=False)

        if isinstance(hid_init, Layer):
            self.hid_init = hid_init
        else:
            self.hid_init = self.add_param(
                hid_init, (1, self.num_units), name="hid_init",
                trainable=learn_init, regularizable=False)

        self.It_init = self.add_param(
            It_init, (1, self.num_dims), name="It_init",
            trainable=learn_init, regularizable=False
        )

    def get_output_shape_for(self, input_shapes):
        input_shape = input_shapes[0]
        return input_shape[0], input_shape[1], self.num_dims

    def get_output_for(self, inputs, **kwargs):
        # Retrieve the layer input
        input = inputs[0]
        # Retrieve the mask when it is supplied
        mask = None
        hid_init = None
        cell_init = None
        visual_input = None
        if self.mask_incoming_index > 0:
            mask = inputs[self.mask_incoming_index]
        if self.hid_init_incoming_index > 0:
            hid_init = inputs[self.hid_init_incoming_index]
        if self.cell_init_incoming_index > 0:
            cell_init = inputs[self.cell_init_incoming_index]
        if self.visual_input_index > 0:
            visual_input = inputs[self.visual_input_index]

        # Treat all dimensions after the second as flattened feature dimensions
        if input.ndim > 3:
            input = T.flatten(input, 3)

        # Because scan iterates over the first dimension we dimshuffle to
        # (n_time_steps, n_batch, n_features)
        input = input.dimshuffle(1, 0, 2)
        seq_len, num_batch, _ = input.shape

        # Stack input weight matrices into a (num_inputs, 4*num_units)
        # matrix, which speeds up computation
        W_in_stacked = T.concatenate(
            [self.W_in_to_ingate, self.W_in_to_forgetgate,
             self.W_in_to_cell, self.W_in_to_outgate, self.W_in_to_ggate],
            axis=1
        )

        # Same for hidden weight matrices
        # pdb.set_trace()
        W_hid_stacked = T.concatenate(
            [self.W_hid_to_ingate, self.W_hid_to_forgetgate,
             self.W_hid_to_cell, self.W_hid_to_outgate, self.W_hid_to_ggate],
            axis=1
        )

        # Stack biases into a (4*num_units) vector
        b_stacked = T.concatenate(
            [self.b_ingate, self.b_forgetgate,
             self.b_cell, self.b_outgate, self.b_ggate], axis=0)

        if self.precompute_input:
            # Because the input is given for all time steps, we can
            # precompute_input the inputs dot weight matrices before scanning.
            # W_in_stacked is (n_features, 4*num_units). input is then
            # (n_time_steps, n_batch, 4*num_units).
            input = T.dot(input, W_in_stacked) + b_stacked

        # When theano.scan calls step, input_n will be (n_batch, 4*num_units).
        # We define a slicing function that extract the input to each LSTM gate
        def slice_w(x, n):
            return x[:, n*self.num_units:(n+1)*self.num_units]

        # Create single recurrent computation step function
        # input_n is the n'th vector of the input
        def step(
            input_n,
            cell_previous, hid_previous,
            visual,
            W_hid_stacked, W_in_stacked, b_stacked,
            W_cell_to_ingate, W_cell_to_forgetgate, W_cell_to_outgate,
            W_h_to_attenGate, W_g_to_attenGate, W_v_to_attenGate, W_s_to_attenGate,
            W_p
        ):
            if not self.precompute_input:
                input_n = T.dot(input_n, W_in_stacked) + b_stacked

            # Calculate gates pre-activations and slice
            gates = input_n + T.dot(hid_previous, W_hid_stacked)

            # Clip gradients
            if self.grad_clipping:
                gates = theano.gradient.grad_clip(
                    gates, -self.grad_clipping, self.grad_clipping)

            # Extract the pre-activation gate values
            ingate = slice_w(gates, 0)
            forgetgate = slice_w(gates, 1)
            cell_input = slice_w(gates, 2)
            outgate = slice_w(gates, 3)
            ggate = slice_w(gates, 4)

            if self.peepholes:
                # Compute peephole connections
                ingate += cell_previous*W_cell_to_ingate
                forgetgate += cell_previous*W_cell_to_forgetgate

            # Apply nonlinearities
            ingate = self.nonlinearity_ingate(ingate)
            forgetgate = self.nonlinearity_forgetgate(forgetgate)
            cell_input = self.nonlinearity_cell(cell_input)
            # ggate gt
            ggate = self.nonlinearity_ggate(ggate)

            # Compute new cell value
            cell = forgetgate*cell_previous + ingate*cell_input

            if self.peepholes:
                outgate += cell*W_cell_to_outgate
            outgate = self.nonlinearity_outgate(outgate)

            # Compute new hidden unit activation
            hid = outgate*self.nonlinearity(cell)
            st = ggate*self.nonlinearity(cell)

            # zt = T.dot(
            #     self.nonlinearity(
            #         T.dot(visual, W_v_to_attenGate) +
            #         T.dot(
            #             T.dot(hid, W_g_to_attenGate).dimshuffle(0, 1, 'x'),
            #             T.ones((1, self.video_len))
            #         )
            #     ),
            #     W_h_to_attenGate
            # )[:, :, 0]

            # to avoid optimization failure of Tenseor 3D dot vector, we should transform
            # e = A.dot(B) to e = A*B.dimshuffle('x', 'x', 0), e=e.sum(axis=2)
            zt_dot_A = self.nonlinearity(
                T.dot(visual, W_v_to_attenGate) +
                T.dot(
                    T.dot(hid, W_g_to_attenGate).dimshuffle(0, 1, 'x'),
                    T.ones((1, self.video_len))
                )
            )
            zt = zt_dot_A*W_h_to_attenGate.dimshuffle('x', 'x', 0)
            zt = zt.sum(axis=2)

            # vt = T.dot(
            #     self.nonlinearity(
            #         T.dot(
            #             st, W_s_to_attenGate
            #         ) +
            #         T.dot(
            #             hid, W_g_to_attenGate
            #         )
            #     ),
            #     W_h_to_attenGate
            # )

            vt_dot_A = self.nonlinearity(
                T.dot(
                    st, W_s_to_attenGate
                ) +
                T.dot(
                    hid, W_g_to_attenGate
                )
            )
            vt = vt_dot_A*W_h_to_attenGate.dimshuffle('x', 0)
            vt = vt.sum(axis=1)
            vt = vt.dimshuffle(0, 'x')

            alpha_hat_t = self.nonlinearity_attenGate(T.concatenate(
                [zt, vt],
                axis=-1
            ))
            feature = T.concatenate(
                [visual_input, st.dimshuffle(0, 'x', 1)],
                axis=1
            ).dimshuffle(2, 0, 1)
            c_hat_t = T.sum(alpha_hat_t*feature, axis=-1)
            It = T.dot(
                (c_hat_t.T+hid), W_p
            )
            return [cell, hid, It]

        def step_masked(
            input_n, mask_n,
            cell_previous, hid_previous, It_previous,
            visual,
            W_hid_stacked, W_in_stacked, b_stacked,
            W_cell_to_ingate, W_cell_to_forgetgate, W_cell_to_outgate,
            W_h_to_attenGate, W_g_to_attenGate, W_v_to_attenGate, W_s_to_attenGate,
            W_p
        ):
            cell, hid, It = step(
                input_n,
                cell_previous, hid_previous,
                visual,
                W_hid_stacked, W_in_stacked, b_stacked,
                W_cell_to_ingate, W_cell_to_forgetgate, W_cell_to_outgate,
                W_h_to_attenGate, W_g_to_attenGate, W_v_to_attenGate, W_s_to_attenGate,
                W_p
            )

            # Skip over any input with mask 0 by copying the previous
            # hidden state; proceed normally for any input with mask 1.
            cell = T.switch(mask_n, cell, cell_previous)
            hid = T.switch(mask_n, hid, hid_previous)
            It = T.switch(mask_n, It, It_previous)
            # theano.printing.Print('It')(It)
            return [cell, hid, It]

        if mask is not None:
            # mask is given as (batch_size, seq_len). Because scan iterates
            # over first dimension, we dimshuffle to (seq_len, batch_size) and
            # add a broadcastable dimension
            mask = mask.dimshuffle(1, 0, 'x')
            sequences = [input, mask]
            step_fun = step_masked
        else:
            sequences = input
            step_fun = step

        ones = T.ones((num_batch, 1))
        if not isinstance(self.cell_init, Layer):
            # Dot against a 1s vector to repeat to shape (num_batch, num_units)
            cell_init = T.dot(ones, self.cell_init)

        if not isinstance(self.hid_init, Layer):
            # Dot against a 1s vector to repeat to shape (num_batch, num_units)
            hid_init = T.dot(ones, self.hid_init)

        It_init = T.dot(ones, self.It_init)

        # The hidden-to-hidden weight matrix is always used in step
        non_seqs = [visual_input, W_hid_stacked]
        if not self.precompute_input:
            non_seqs += [W_in_stacked, b_stacked]
        else:
            non_seqs += [(), ()]
        # The "peephole" weight matrices are only used when self.peepholes=True
        if self.peepholes:
            non_seqs += [self.W_cell_to_ingate,
                         self.W_cell_to_forgetgate,
                         self.W_cell_to_outgate]
        else:
            non_seqs += [(), (), ()]

        # When we aren't precomputing the input outside of scan, we need to
        # provide the input weights and biases to the step function

        non_seqs += [self.W_h_to_attenGate, self.W_g_to_attenGate, self.W_v_to_attenGate, self.W_s_to_attenGate, self.W_p]

        if self.unroll_scan:
            # Retrieve the dimensionality of the incoming layer
            input_shape = self.input_shapes[0]
            # Explicitly unroll the recurrence instead of using scan
            cell_out, hid_out, It = unroll_scan(
                fn=step_fun,
                sequences=sequences,
                outputs_info=[cell_init, hid_init, It_init],
                go_backwards=self.backwards,
                non_sequences=non_seqs,
                n_steps=input_shape[1])
        else:
            # Scan op iterates over first dimension of input and repeatedly
            # applies the step function

            cell_out, hid_out, It = theano.scan(
                fn=step_fun,
                sequences=sequences,
                outputs_info=[cell_init, hid_init, It_init],
                go_backwards=self.backwards,
                truncate_gradient=self.gradient_steps,
                non_sequences=non_seqs,
                strict=True)[0]

        It = It.dimshuffle(1, 0, 2)
        if self.backwards:
            It = It[:, ::-1]
        return It
