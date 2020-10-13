from openfermion.transforms import get_fermion_operator, jordan_wigner, get_sparse_operator
from openfermionpsi4 import run_psi4
from openfermion.hamiltonians import MolecularData
from openfermion.utils import freeze_orbitals

import src.backends as backends
from src.utils import QasmUtils, LogUtils
from src import config

import scipy
import numpy
import time
from functools import partial

import logging

from src.ansatz_element_lists import UCCSD
import ray


# TODO make this class entirely static?
class VQERunner:
    # Works for a single geometry
    def __init__(self, q_system, backend=backends.QiskitSim, optimizer=config.optimizer, use_cache=True,
                 optimizer_options=config.optimizer_options, print_var_parameters=False, use_ansatz_gradient=False):

        self.backend = backend
        self.optimizer = optimizer
        self.optimizer_options = optimizer_options
        self.use_ansatz_gradient = use_ansatz_gradient
        self.print_var_parameters = print_var_parameters
        self.use_cache = use_cache

        self.q_system = q_system

        self.previous_energy = self.q_system.hf_energy
        self.new_energy = None

        self.iteration = 0
        self.time_previous_iter = 0

    # TODO split this into a proper callback function!!!!!!
    def get_energy(self, var_parameters, ansatz, backend, multithread=False, multithread_iteration=None,
                   init_state_qasm=None, cache=None, excited_state=0):

        if multithread is False:
            iteration_duration = time.time() - self.time_previous_iter
            self.time_previous_iter = time.time()

        energy = backend.expectation_value(self.q_system.jw_qubit_ham, ansatz, var_parameters, self.q_system,
                                           cache=cache, init_state_qasm=init_state_qasm, excited_state=excited_state)

        if multithread:
            if multithread_iteration is not None:
                try:
                    multithread_iteration[0] += 1
                except TypeError as te:
                    logging.warning(te)
        else:
            self.new_energy = energy
            delta_e = self.new_energy - self.previous_energy
            self.previous_energy = self.new_energy

            message = 'Iteration: {}. Energy {}.  Energy change {} , Iteration dutation: {}' \
                .format(self.iteration, self.new_energy, '{:.3e}'.format(delta_e), iteration_duration)
            if self.print_var_parameters:
                message += ' Params: ' + str(var_parameters)
            logging.info(message)

            self.iteration += 1

        return energy

    # def get_ansatz_gradient(self, var_parameters, ansatz, backend, init_state_qasm=None):
    #     return backend.ansatz_gradient(var_parameters, ansatz=ansatz, init_state_qasm=init_state_qasm)

    def vqe_run(self, ansatz, initial_var_parameters=None, init_state_qasm=None, excited_state=0):

        assert len(ansatz) > 0
        if initial_var_parameters is None:
            var_parameters = numpy.zeros(sum([element.n_var_parameters for element in ansatz]))
        else:
            assert len(initial_var_parameters) == sum([element.n_var_parameters for element in ansatz])
            var_parameters = initial_var_parameters

        message = ''
        message += '-----Running VQE for: {}-----\n'.format(self.q_system.name)
        message += '-----Number of electrons: {}-----\n'.format(self.q_system.n_electrons)
        message += '-----Number of orbitals: {}-----\n'.format(self.q_system.n_orbitals)
        message += '-----Numeber of ansatz elements: {}-----\n'.format(len(ansatz))
        if len(ansatz) == 1:
            message += '-----Ansatz type {}------\n'.format(ansatz[0].element)
        message += '-----Statevector and energy calculated using {}------\n'.format(self.backend)
        message += '-----Optimizer {}------\n'.format(self.optimizer)
        logging.info(message)

        self.iteration = 1
        self.time_previous_iter = time.time()

        # initialize the cache if running on Qiskit simulator
        cache = None
        if self.use_cache and self.backend == backends.QiskitSim:
            cache = backends.QiskitSimCache(self.q_system)
            if excited_state > 0:
                # calculate the lower energetic statevectors
                cache.calculate_hamiltonian_for_excited_state(self.q_system.H_lower_state_terms,
                                                              excited_state=excited_state)

        # TODO move to the cache class
        # precompute frequently used quantities
        if self.use_ansatz_gradient:
            for element in ansatz:
                element.calculate_excitation_generator_matrix()  # the excitation matrices are now computed and stored in each element

        get_energy = partial(self.get_energy, ansatz=ansatz, backend=self.backend, init_state_qasm=init_state_qasm,
                             excited_state=excited_state, cache=cache)

        get_gradient = partial(self.backend.ansatz_gradient, ansatz=ansatz, q_system=self.q_system,
                               init_state_qasm=init_state_qasm, cache=cache)

        if self.use_ansatz_gradient:
            opt_energy = scipy.optimize.minimize(get_energy, var_parameters, jac=get_gradient, method=self.optimizer,
                                                 options=self.optimizer_options, tol=config.optimizer_tol,
                                                 bounds=config.optimizer_bounds)
        else:

            opt_energy = scipy.optimize.minimize(get_energy, var_parameters, method=self.optimizer,
                                                 options=self.optimizer_options, tol=config.optimizer_tol,
                                                 bounds=config.optimizer_bounds)

        # Prevents memory overflow with ray
        for element in ansatz:
            element.delete_excitation_generator_matrix()

        logging.info(opt_energy)

        opt_energy['n_iters'] = self.iteration  # cheating
        return opt_energy

    @ray.remote
    def vqe_run_multithread(self, ansatz, initial_var_parameters=None, init_state_qasm=None):

        assert len(ansatz) > 0

        if initial_var_parameters is None or initial_var_parameters == []:
            var_parameters = numpy.zeros(sum([element.n_var_parameters for element in ansatz]))
        else:
            assert len(initial_var_parameters) == sum([element.n_var_parameters for element in ansatz])
            var_parameters = initial_var_parameters

        # create it as a list so we can pass it by reference
        local_thread_iteration = [0]

        backend = self.backend(self.q_system)

        # precomputed excitation matrices.
        if self.use_ansatz_gradient:
            for element in ansatz:
                element.calculate_excitation_generator_matrix()

        get_energy = partial(self.get_energy, ansatz=ansatz, backend=backend, init_state_qasm=init_state_qasm,
                             multithread=True, multithread_iteration=local_thread_iteration)

        get_gradient = partial(self.backend.ansatz_gradient, ansatz=ansatz, init_state_qasm=init_state_qasm)

        if self.use_ansatz_gradient:
            opt_energy = scipy.optimize.minimize(get_energy, var_parameters, method=self.optimizer, jac=get_gradient,
                                                 options=self.optimizer_options, tol=config.optimizer_tol,
                                                 bounds=config.optimizer_bounds)
        else:
            opt_energy = scipy.optimize.minimize(get_energy, var_parameters, method=self.optimizer,
                                                 options=self.optimizer_options, tol=config.optimizer_tol,
                                                 bounds=config.optimizer_bounds)

        if len(ansatz) == 1:
            message = 'Ran VQE for element {}. Energy {}. Iterations {}'.format(ansatz[0].element,
                                                                                opt_energy.fun, local_thread_iteration[0])
            # logging.info(message)
            print(message)
        else:
            message = 'Ran VQE. Energy {}. Iterations {}'.format(opt_energy.fun, local_thread_iteration[0])
            # logging.info(message)
            print(message)

        # Prevents memory overflow with ray
        for element in ansatz:
            element.delete_excitation_generator_matrix()

        opt_energy['n_iters'] = local_thread_iteration[0]  # cheating
        return opt_energy
