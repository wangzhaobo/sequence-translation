import theano
import theano.tensor as T

import keras.optimizers as optimizers
import keras.objectives as objectives
from keras.utils.generic_utils import Progbar
from keras.models import *
from keras import activations, initializations
from keras.utils.theano_utils import shared_zeros, alloc_zeros_matrix, sharedX
from keras.layers.core import Layer

from itertools import izip, chain

import numpy

from mem import *
from flat import *

class JointModel(object):
    '''
        JointModel: create train/compile/test functions that connect
        two models. Currently the only supported pairing is
        SequentialSequence -> RecurrentSequence

    '''
    def __init__(self, *models):
        assert len(models) == 2
        self.models = models
        self.model_A = models[0]
        self.model_B = models[1]

    def get_weights(self):
        return [m.get_weights() for m in self.models]

    def set_weights(self, weights):
        assert len(self.models) == len(weights)
        for m, p in izip(self.models, weights):
            m.set_weights(p)

    def connect(self, train=False):
        '''
            Connects model_B's input to model_A's output.
            Currently makes a lot of assumptions on what model_A and model_B are.

        '''
        output_A, memories_A = self.model_A.layers[-1].output(train=train)
        for mem_A, layer_B in izip(memories_A, self.model_B.layers) :
            layer_B.C1 = mem_A

    def compile(self, optimizer, loss, log_fcn=lambda x, y: (x, y),
            joint_model=True, compile_train=True):
        '''
            Compile both models, then wrap both inside of a theano function.
            Currently makes a lot of assumptions on what model_A and model_B are.

            Some of the compiled functions here might be redundant; this
            section should be cleaned up.

        '''

        log = lambda x: log_fcn(x, True)
        joint_model = True

        log("Entering compile...")
        log("Creating predict/test functions (train=False)...")

        self.connect(train=False)

        log("Compiling model A...")
        self.model_A.compile(optimizer, loss,
                log_fcn=lambda x, y: log_fcn("model A: " + x, y),
                joint_model=True, skiplist=['train'])

        log("Compiling model B...")
        self.model_B.compile(optimizer, loss,
                log_fcn=lambda x, y: log_fcn("model B: " + x, y),
                joint_model=True, skiplist=['train'])

        log("Creating predict function...")
        self.__predict = theano.function([self.model_A.X] + 
                [self.model_B.X1] + 
                [layer.H1 for layer in self.model_B.layers],
                self.model_B.predict_result,
                allow_input_downcast=True)

        log("Creating test function...")
        self.__test = theano.function([self.model_A.X, self.model_B.Y, self.model_B.M] + 
                [self.model_B.X1] + 
                [layer.H1 for layer in self.model_B.layers],
                self.model_B.test_score,
                allow_input_downcast=True)


        if compile_train:
            log("Creating train function function (train=True)...")

            self.connect(train=True)

            log("Compiling model A...")
            self.model_A.compile(optimizer, loss,
                    log_fcn=lambda x, y: log_fcn("model A: " + x, y),
                    joint_model=True, skiplist=['predict', 'test'])

            log("Compiling model B...")
            self.model_B.compile(optimizer, loss,
                    log_fcn=lambda x, y: log_fcn("model B: " + x, y),
                    joint_model=True, skiplist=['predict', 'test'])

            log("Setting model A's updates...")
            self.model_A.updates = self.model_A.optimizer.get_updates( \
                    self.model_A.params, self.model_B.train_loss)
            
            log("Creating train function...")
            self.__train = theano.function([self.model_A.X, self.model_B.Y, self.model_B.M] + 
                    [self.model_B.X1] + 
                    [self.model_A.lr] +
                    [self.model_B.lr] +
                    [layer.H1 for layer in self.model_B.layers],
                    self.model_B.train_loss,
                    updates=self.model_A.updates + self.model_B.updates,
                    allow_input_downcast=True)
        else:
            log("Skipped creating train function")
            self.__train = None

        log("Done compiling functions")

    def _train(self, X, y, M, lr_A, lr_B):
        '''
            Wrapper to compiled train function, called by other member functions.

        '''
        if lr_A is None:
            lr_A = self.model_A.old_lr
        if lr_B is None:
            lr_B = self.model_B.old_lr
        X = [X]
        y = [y]
        M = [M]
        X1 = [[self.X1[0]]*len(X[0])]
        LR = [lr_A, lr_B]
        H = [numpy.zeros((len(X[0]), layer.output_dim), dtype=numpy.float32)
                for layer in self.model_B.layers]
        return self.__train(*(X + y + M + X1 + LR + H))

    def _predict(self, X):
        '''
            Wrapper to compiled predict function, called by other member functions.

        '''
        X = [X]
        X1 = [[self.X1[0]]*len(X[0])]
        H = [numpy.zeros((len(X[0]), layer.output_dim), dtype=numpy.float32)
                for layer in self.model_B.layers]
        return self.__predict(*(X + X1 + H))

    def _test(self, X, y, M):
        '''
            Wrapper to compiled test function, called by other member functions.

        '''
        X = [X]
        y = [y]
        M = [M]
        X1 = [[self.X1[0]]*len(X[0])]
        H = [numpy.zeros((len(X[0]), layer.output_dim), dtype=numpy.float32)
                for layer in self.model_B.layers]
        return self.__test(*(X + y + M + X1 + H))

    def train(self, X, y, M, lr_A, lr_B):
        y = standardize_y(y)
        loss = self._train(X, y, M, lr_A, lr_B)
        return loss

    def test(self, X, y, M):
        y = standardize_y(y)
        score = self._test(X, y, M)
        return score

    def fit(self, X, y, M, batch_size=128, nb_epoch=100, verbose=1, shuffle=True,
            lr_A=None, lr_B=None,
            validation_split=0., validation_skip=0, validation_callback=None):

        y = standardize_y(y)

        # If a validation split size is given (e.g. validation_split=0.2)
        # then split X into smaller X and X_val,
        # and split y into smaller y and y_val.
        do_validation = False
        if validation_split > 0 and validation_split < 1:
            do_validation = True
            split_at = int(len(X) * (1 - validation_split))
            (X, X_val) = (X[0:split_at], X[split_at:])
            (y, y_val) = (y[0:split_at], y[split_at:])
            (M, M_val) = (M[0:split_at], M[split_at:])
            if verbose:
                print "Train on %d samples, validate on %d samples" % (len(y), len(y_val))
                print "validation_split", validation_split
                print "validation_skip", validation_skip, "validation_callback", (validation_callback is not None)
        
        index_array = numpy.arange(len(X))
        for epoch in range(nb_epoch):
            if verbose:
                print 'Epoch', epoch
            if shuffle:
                numpy.random.shuffle(index_array)

            nb_batch = int(numpy.ceil(len(X)/float(batch_size)))
            progbar = Progbar(target=len(X))
            for batch_index in range(0, nb_batch):
                batch_start = batch_index*batch_size
                batch_end = min(len(X), (batch_index+1)*batch_size)
                batch_ids = index_array[batch_start:batch_end]

                X_batch = X[batch_ids]
                y_batch = y[batch_ids]
                M_batch = M[batch_ids]
                loss = self._train(X_batch, y_batch, M_batch, lr_A, lr_B)
                
                if verbose:
                    is_last_batch = (batch_index == nb_batch - 1)
                    m = 10000
                    if not is_last_batch or not do_validation:
                        progbar.update(batch_end, [('loss', loss*m)])
                    else:
                        progbar.update(batch_end, [('loss', loss*m), ('val. loss', self.test(X_val, y_val, M_val)*m)])

            # print validation/debug output every validation_skip epochs
            if validation_callback and validation_skip > 0 and not (epoch + 1) % validation_skip:
                print "begin validation_callback:"
                sets = []
                sets.append(('train', X, y))
                if do_validation:
                    sets.append(('validate', X_val, y_val))
                validation_callback(sets, epoch)
                print "end validation_callback"

    def predict_batch(self, X, batch_size=128):
        '''
            Predict function. Expects and returns 3D tensors of shape
            [embedding size] * [sequence length] * sequences

        '''
        preds = None
        for batch_index in range(0, len(X)/batch_size+1):
            batch = range(batch_index*batch_size, min(len(X), (batch_index+1)*batch_size))
            if not batch:
                break
            batch_preds = self._predict(X[batch[0]:batch[-1]+1])

            if batch_index == 0:
                shape = (len(X),) + batch_preds.shape[1:]
                preds = numpy.zeros(shape)
            preds[batch] = batch_preds
        return preds
