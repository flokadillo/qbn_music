"""
This script contains the code for implementing variational inference based
on the paper "Variational inference with a quantum computer" by Marcello
Benedetti et al., 2021.
"""
__author__ = "Florian Krebs"
from sprinkler_example import create_graph as create_sprinkler_graph
from qiskit.circuit.library import EfficientSU2, RealAmplitudes
from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator
import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.callbacks import EarlyStopping
from copy import deepcopy
import matplotlib.pyplot as plt
from qubayes.qubayes_tools import Node, Graph
import itertools
from qiskit_algorithms import optimizers
from qubayes_tools import BayesNet


def logit(x):
    eps = 1e-5
    if x > (1 - eps):
        x = 1 - eps
    elif x < eps:
        x = eps
    return np.log(x / (1 - x))


def counts_to_cpd(counts, reverse=True):
    # counts come from result.get_counts() and are: {'111': 137, '101': 136, '010': 118} S, R, C
    n_dim = len(list(counts.keys())[0])
    n_samples = sum(counts.values())
    lst = list(itertools.product([0, 1], repeat=n_dim))
    cpd = np.zeros((2,) * n_dim, dtype=float) # cpd is C, R, S
    states = np.array(list(counts.keys()))
    for c in lst:
        if reverse:  #
            idx = c[::-1]  #
        else:
            idx = c
        key = "".join([str(x) for x in idx])
        if key in list(counts.keys()):
            cpd[c] = float(counts[key]) / n_samples
    return cpd


class OptimalClassifier(object):

    def __init__(self, bayes_net):
        self.bn = bayes_net
        self.q_posterior = None
        self.p_prior = bayes_net.compute_p_prior()

    def train(self, train_x, train_y, learning_rate=None):
        # learn q(C, R, S | W = 1) from train_x
        train_x = train_x[train_y == 0, :]  # get only samples from born machine
        unique_rows, unique_counts = np.unique(train_x, axis=0, return_counts=True)
        # TODO: this works only for a single evidence variable
        lst = list(itertools.product([0, 1], repeat=self.bn.graph.n_variables-1))
        estimation = np.zeros((2,) * (self.bn.graph.n_variables-1), dtype=float)
        for c in lst:
            idx = (unique_rows == np.array([c])).all(axis=1)
            if idx.any():
                estimation[c] = float(unique_counts[idx][0]) / train_x.shape[0]
        self.q_posterior = estimation
        return None

    def predict(self, samples, labels=None):
        # According to Eq. 5, predict p_prior
        if labels is None:
            labels = np.zeros((samples.shape[0],))
        self.train(samples, labels)  # update q_crs using samples from bm (class 0)
        pred = np.zeros((samples.shape[0],))
        for i in range(samples.shape[0]):
            idx = tuple(samples[i, :])
            pred[i] = self.q_posterior[idx] / (self.q_posterior[idx] + self.p_prior[idx])
        return pred

    def compute_loss(self, train_x, train_y):
        # According to Eq. 4
        # 0 ... born machine, 1 ... prior
        p_prior = self.predict(train_x, labels=train_y)
        # born machine samples
        E_log_bm = (np.log(1 - p_prior[train_y == 0])).mean()
        # prior
        E_log_prior = (np.log(p_prior[train_y == 1])).mean()
        return E_log_bm + E_log_prior


class MLP_Classifier(object):

    def __init__(self, n_inputs):
        self.model = tf.keras.Sequential([
            layers.Input(shape=(n_inputs,)),
            layers.Dense(6, activation='relu'),
            layers.Dense(1, activation='sigmoid')
        ])

    def train(self, train_x, train_y, learning_rate=0.03):
        # shuffle datasets
        idx = np.random.permutation(train_x.shape[0])
        train_x = train_x[idx, :]
        train_y = train_y[idx]
        split = 0.2
        idx = int(train_x.shape[0] * split)
        val_x = train_x[:idx, :]
        val_y = train_y[:idx]
        train_x = train_x[idx:, :]
        train_y = train_y[idx:]

        optimizer = tf.keras.optimizers.Adam(learning_rate=learning_rate)
        # Define the early stopping callback
        early_stopping = EarlyStopping(
            monitor='val_loss',         # Monitor validation loss
            patience=20,                # Wait for 5 epochs of no improvement
            restore_best_weights=True   # Restore the model to the best epoch
        )
        self.model.compile(optimizer=optimizer,
                           loss=tf.keras.losses.BinaryCrossentropy(from_logits=False),  # we already have a sigmoid after the last layer
                           metrics=['accuracy'])
        history = self.model.fit(x=train_x, y=train_y,
                                 validation_data=(val_x, val_y),
                                 epochs=200, batch_size=10,
                                 verbose=0, callbacks=[early_stopping])

        # Calculate the best epoch index
        best_epoch = early_stopping.stopped_epoch - early_stopping.patience + 1

        # Retrieve the validation accuracy of the best epoch
        best_val_accuracy = history.history['val_accuracy'][best_epoch]
        print(f"- Validation accuracy {best_val_accuracy:.2f} at best epoch {best_epoch}.")

        return history

    def compute_loss(self, train_x, train_y):
        # According to Eq. 4
        # 0 ... born machine, 1 ... prior
        p_prior = self.predict(train_x)
        # born machine samples
        E_log_bm = (np.log(1 - p_prior[train_y == 0])).mean()
        # prior
        E_log_prior = (np.log(p_prior[train_y == 1])).mean()
        return E_log_bm + E_log_prior

    def predict(self, samples):
        # if prob > 0.5 => class = 1 (prior)
        return self.model.predict(samples, verbose=0)[:, 0]  # return 1d array


class Optimizer(object):

    def __init__(self, born_machine, bayes_net, classifier, n_iterations=100, learning_rate=0.003):
        self.n_iterations = n_iterations
        self.learning_rate = learning_rate
        self.bayes_net = bayes_net
        self.classifier = classifier
        self.born_machine = born_machine

    def estimate_gradient(self, n_samples=100):
        # Use parameter shift rule for estimation, as outlined in B15 in the paper
        shift = np.pi / 2
        gradients = np.zeros(self.born_machine.params.shape)

        for i in range(len(self.born_machine.params)):
            # Original parameters
            bm = {'plus': deepcopy(self.born_machine),
                  'minus': deepcopy(self.born_machine)}
            # apply shifts
            bm['plus'].params[i] += shift
            bm['minus'].params[i] -= shift
            md = dict()

            for key in ['plus', 'minus']:
                # Sample 50 points
                samples_crs = bm[key].sample(n_samples)
                # classify 50 points
                # TODO: Really logit?
                p_prior = self.classifier.predict(samples_crs)
                p_bm = 1. - p_prior
                # compute P(x|z) for the 50 points
                loglik = self.bayes_net.compute_log_likelihood(samples_crs)
                # compute the mean difference (logit(d_i) - log(p(x_i|z_i))) ->
                md[key] = (logit(p_bm) - loglik).mean()

            # compute the gradient as (md_plus - md_minus) / 2
            gradients[i] = (md['plus'] - md['minus']) / 2
        return gradients

    def compute_kl_loss(self, samples_crs=None):
        # Compute L_KL loss as defined in Eq. 7.
        if samples_crs is None:
            samples_crs = self.born_machine.sample(100)
        p_prior = self.classifier.predict(samples_crs)
        p_born = 1. - p_prior
        loglik = self.bayes_net.compute_log_likelihood(samples_crs)
        return (logit(p_born) - loglik).mean()

    def optimize(self):
        metrics = {'tvd': np.zeros((self.n_iterations,)),
                   'kl_loss': np.zeros((self.n_iterations,)),
                   'ce_loss': np.zeros((self.n_iterations,))}
        for i in range(self.n_iterations):
            # Draw 100 sample from the born machine
            s_bm = self.born_machine.sample(100)
            s_prior = self.bayes_net.sample_from_prior(100)

            # Train Classifier with {S_prior + S_born} to distinguish prior and born machine
            x_train = np.vstack((s_bm, s_prior))
            y_train = np.zeros((s_prior.shape[0] + s_bm.shape[0],))  # 0 ... born machine, 1 ... prior
            y_train[s_bm.shape[0]:] = 1
            history = self.classifier.train(x_train, y_train)

            # plt.figure()
            # plt.plot(history.history['loss'])
            # plt.plot(history.history['val_loss'])
            # plt.legend(['train_loss', 'val_loss'])
            # plt.savefig(fr'C:\Users\krf\projects\Quanco\git\qubayes\figs\vi_classifier\learning_curve_it{i}.png')

            # Calculate the gradient using the parameter-shift rule
            gradients = self.estimate_gradient()

            # Update the parameter (for maximization, we add the gradient)
            self.born_machine.params += self.learning_rate * gradients

            # Evaluate the function at the new theta
            metrics['kl_loss'][i] = self.compute_kl_loss(s_bm)
            metrics['tvd'][i] = self.compute_tvd(s_bm)
            metrics['ce_loss'][i] = self.classifier.compute_loss(x_train, y_train)

            # Print the current theta and function value
            print(f"Iteration {i + 1}: tvd = {metrics['tvd'][i]:.4f}, born loss = {metrics['kl_loss'][i]:.4f},"
                  f" clf loss = {metrics['ce_loss'][i]:.4f}")
        return self.born_machine, metrics

    def compute_tvd(self, samples):
        # Compute total variation distance (The largest absolute difference
        # between the probabilities that the two probability distributions
        # assign to the same event.).
        return self.bayes_net.compute_tvd(samples)


class DerivativeFreeOptimizer(object):

    def __init__(self, born_machine, bayes_net, classifier, n_iterations=100,
                 learning_rate=0.003, method='COBYLA'):
        self.n_iterations = n_iterations
        self.learning_rate = learning_rate
        self.bayes_net = bayes_net
        self.classifier = classifier
        self.born_machine = born_machine
        self.method = method
        self.params = None

    def compute_kl_loss(self, theta_values):
        n_samples = 1000
        self.born_machine.params = theta_values
        samples = self.born_machine.sample(n_samples, return_samples=True)
        q_bm = self.born_machine.q_bm
        p_prior = self.bayes_net.compute_p_prior()
        logliks = self.bayes_net.compute_log_likelihood(samples)
        loss = 0
        for i in range(samples.shape[0]):
            s = samples[i, :]
            # If q and P(z|x) match, the loss should be np.log(self.bayes_net.compute_p_wet(wet=1))
            d_bm = q_bm[tuple(s)] / (q_bm[tuple(s)] + p_prior[tuple(s)])  # optimal classifier
            loss += (logit(d_bm) - logliks[i])
            # loss += np.log(q_bm[tuple(s)] / p_prior[tuple(s)]) - logliks[i]
        return loss / samples.shape[0]

    def optimize(self):

        optimizer = getattr(optimizers, self.method)
        info = {'parameters': [], 'loss': []}

        if self.method == 'COBYLA':
            def callback(parameters):  # storing intermediate info
                info['parameters'].append(parameters)
        elif self.method == 'GradientDescent':
            def callback(nfev, parameters, loss_value, gradient_norm):  # storing intermediate info
                info['parameters'].append(parameters)
                info['loss'].append(loss_value)
        opt = optimizer(maxiter=self.n_iterations, callback=callback, tol=1e-10)
        opt_results = opt.minimize(self.compute_kl_loss, self.born_machine.params)
        n_iterations = len(info['parameters'])
        metrics = {'tvd': np.zeros(n_iterations),
                   'kl_loss': np.zeros(n_iterations),
                   'ce_loss': np.zeros(n_iterations)}
        # posterior = self.bayes_net.compute_posterior()
        for i in range(len(info['parameters'])):
            # Print progress
            metrics['kl_loss'][i] = self.compute_kl_loss(info['parameters'][i])
            self.born_machine.params = info['parameters'][i]
            pred = self.born_machine.sample(1000, return_samples=False)
            metrics['tvd'][i] = self.compute_tvd()
            posterior = self.bayes_net.compute_posterior()
            print(f"Iteration {i + 1}: Loss = {metrics['kl_loss'][i]:.4f}, "
                  f"Pred = {pred.flat[0]:.3f}, "
                  f"True = {posterior.flat[0]:.3f}")

        return self.born_machine, metrics

    def compute_tvd(self):
        posterior = self.bayes_net.compute_posterior()
        tvd = (abs(posterior - self.born_machine.q_bm)).max()
        return tvd


class BornMachine(object):

    def __init__(self, n_qubits, n_blocks=0, ansatz_type='RealAmplitudes'):
        # n_blocks is L in the paper
        self.n_qubits = n_qubits
        self.n_blocks = n_blocks
        self.params = None
        self.ansatz = None
        self.q_bm = None
        self.ansatz_type = ansatz_type
        self.reset_ansatz()

    def reset_ansatz(self):
        if self.ansatz_type == 'RealAmplitudes':
            self.ansatz = RealAmplitudes(self.n_qubits, reps=self.n_blocks,
                                         entanglement='linear')
        else:
            self.ansatz = EfficientSU2(self.n_qubits, su2_gates=['rz', 'rx'],
                                       reps=self.n_blocks,
                                       entanglement='linear')
        if self.params is None:
            self.params = np.random.normal(0, 0.1, size=self.ansatz.num_parameters)
        param_dict = {param: value for param, value in zip(self.ansatz.parameters, self.params)}
        self.ansatz.assign_parameters(param_dict, inplace=True)

    def print_circuit(self):
        print(self.ansatz.decompose())

    def sample(self, n_samples, return_samples=True):
        # Create a quantum circuit
        circuit = QuantumCircuit(self.n_qubits)
        # Apply a Hadamard gate to each qubit for state preparation
        for qubit in range(self.n_qubits):
            circuit.h(qubit)
        self.reset_ansatz()
        circuit.compose(self.ansatz, inplace=True)
        circuit.measure_all()

        # Simulate the circuit
        simulator = AerSimulator(method='matrix_product_state')
        compiled_circuit = transpile(circuit, simulator)
        result = simulator.run(compiled_circuit, shots=n_samples, memory=True).result()
        counts = result.get_counts()
        self.q_bm = counts_to_cpd(counts, reverse=True)
        if return_samples:
            samples = result.get_memory()
            out = np.array([[char == '1' for char in string[::-1]] for string in samples], dtype='int32')
            # check
            unique_rows, counts = np.unique(out, axis=0, return_counts=True)
            if len(counts) > 1:
                np.testing.assert_almost_equal(counts[1]/n_samples, self.q_bm[tuple(unique_rows[1])], decimal=3)
        else:
            out = self.q_bm
        return out


class SimpleBN(BayesNet):

    def __init__(self):
        rain = Node('rain', data=np.array([0.5, 0.5]))
        wet = Node('wet', data=np.array([[0.8, 0.1],
                                         [0.2, 0.9]]),
                   parents=['rain'])
        graph = Graph({'rain': rain, 'wet': wet})
        super().__init__(graph)
        return

    def compute_p_prior(self):
        return self.graph.marginalize_all_but(['rain'])

    def compute_log_likelihood(self, samples, wet=1):
        # Compute the log likelihood P(W=1 | R)
        log_lik = np.zeros((samples.shape[0],))
        for i in range(samples.shape[0]):
            r = samples[i, :]
            log_lik[i] = np.log(max([1e-3, self.graph.nodes['wet'].data[wet, r][0]]))
        return log_lik


class SimpleBN2(BayesNet):

    def __init__(self):
        rain = Node('rain', data=np.array([0.3, 0.7]))
        sprinkler = Node('sprinkler', data=np.array([0.8, 0.2]))
        wet = Node('wet', data=np.array([[[0.9, 0.3], [0.2, 0.1]],
                                         [[0.1, 0.7], [0.8, 0.9]]]),
                   parents=['rain', 'sprinkler'])
        graph = Graph({'rain': rain, 'sprinkler': sprinkler, 'wet': wet})
        super().__init__(graph)
        return

    def compute_p_wet(self, wet=1):
        p = self.graph.marginalize_all_but(['wet'])
        return p[wet]

    def compute_p_prior(self):
        # Compute the prior P(R, S)
        return self.graph.marginalize_all_but(['rain', 'sprinkler'])

    def compute_log_likelihood(self, samples, wet=1):
        # Compute the log likelihood P(W=1 | R, S)
        log_lik = np.zeros((samples.shape[0],))
        for i in range(samples.shape[0]):
            (r, s) = samples[i, :]
            log_lik[i] = np.log(max([1e-3, self.graph.nodes['wet'].data[wet, r, s]]))
        return log_lik





def plot_optimization_metrics(metrics, save=False):
    fig, ax = plt.subplots(1, 2, figsize=(13, 4))
    ax[0].plot(metrics['kl_loss'], label='Loss according to Eq. 7')
    ax[0].set_xlabel('Epoch')
    ax[0].set_ylabel('Loss (Eq. 7)')
    ax[0].legend()
    ax[1].plot(metrics['tvd'], label='TVD between q(z|x) and p(z|x)')
    if metrics['ce_loss'].max() > metrics['ce_loss'].min():
        ax[1].plot(metrics['ce_loss'], label='Classifier loss')
    ax[1].set_xlabel('Epoch')
    ax[1].set_ylabel('TVD')
    ax[1].legend()
    if save:
        out_fln = fr'C:\Users\krf\projects\Quanco\git\qubayes\figs\vi_classifier\optimization.png'
        plt.savefig(out_fln)
        print(f'Saved figure to {out_fln}')
    else:
        plt.show()


def main():

    # Create BN object
    bn = SprinklerBN(random_cpd=True)
    # bn = SimpleBN2()

    # Initialize a born machine
    bm = BornMachine(len(bn.graph.nodes)-1, n_blocks=1,
                     ansatz_type='RealAmplitudes')
    # bm = OptimalBornMachine(bn)
    # bm.print_circuit()

    # Classifier
    classifier = OptimalClassifier(bn)
    # classifier = MLP_Classifier(n_inputs=bm.n_qubits)

    # Optimize it
    # opt = Optimizer(bm, bn, classifier, n_iterations=500, learning_rate=0.003)
    opt = DerivativeFreeOptimizer(bm, bn, classifier, n_iterations=400, learning_rate=0.003)
    bm_opt, metrics = opt.optimize()
    plot_optimization_metrics(metrics, save=0)
    return


if __name__ == "__main__":
    main()


