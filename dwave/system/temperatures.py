# Copyright 2022 D-Wave Systems Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

r"""The following effective temperature and bias estimators are provided:

- Maximum pseudo-likelihood is an efficient estimator for the temperature 
  describing a classical Boltzmann distribution P(x) = \exp(-H(x)/T)/Z(T) 
  given samples from that distribution, where H(x) is the classical energy 
  function. The following links describe features of the estimator in
  application to equilibrium distribution drawn from binary quadratic models and
  non-equilibrium distributions generated by annealing:
  https://www.jstor.org/stable/25464568
  https://doi.org/10.3389/fict.2016.00023 

- An effective temperature can be inferred assuming freeze-out during the 
  anneal at s=t/t_a, an annealing schedule, and a device physical temperature.
  Necessary device-specific properties are published for online solvers:
  https://docs.dwavesys.com/docs/latest/doc_physical_properties.html

- The biases (h) equivalent to application of flux bias, or vice-versa,
  can be inferred as a function of the anneal progress s=t/t_a by
  device-specific unit conversion. The necessary parameters for estimation
  [Mafm, B(s)] are published for online solvers:
  https://docs.dwavesys.com/docs/latest/doc_physical_properties.html
""" 

import warnings
import numpy as np
import dimod
from scipy import optimize
from typing import Tuple, Union, Optional, Literal

__all__ = ['effective_field', 'maximum_pseudolikelihood_temperature',
           'freezeout_effective_temperature', 'fast_effective_temperature',
           'Ip_in_units_of_B', 'h_to_fluxbias', 'fluxbias_to_h']

def effective_field(bqm,
                    samples=None,
                    current_state_energy=False) -> (np.ndarray,list):
    r'''Returns the effective field for all variables and all samples.

    The effective field with ``current_state_energy = False`` is the energy
    attributable to setting a variable to value 1, conditioned on fixed values 
    for all neighboring variables (relative to exclusion of the variable, and 
    associated energy terms, from the problem). 

    The effective field with ``current_state_energy = True`` is the energy gained
    by flipping the variable state against its current value (from say -1 to 1 
    in the Ising case, or 0 to 1 in the QUBO case). A positive value indicates 
    that the energy can be decreased by flipping the variable, hence the 
    variable is in a locally excited state.
    If all values are negative  (positive) within a sample, that sample is a 
    local minima (maxima).  

    Any BQM can be converted to an Ising model with

    .. math::
        H(s) = Constant + \sum_i h_i s_i + 0.5 \sum_{i,j} J_{i,j} s_i s_j

    with unique values of :math:`J` (symmetric) and :math:`h`. The sample
    dependent effect field on variable i, :math:`f_i(s)`, is then defined

    if current_state_energy == False:
        .. math::
            f_i(s) = h_i + \sum_j J_{i,j} s_j
    else:
        .. math:: 
            f_i(s) = 2 s_i [h_i + \sum_j J_{i,j}  s_j]

    Args:
        bqm (:obj:`dimod.BinaryQuadraticModel`): 
            Binary quadratic model.
        samples (samples_like or :obj:`~dimod.SampleSet`,optional):
            A collection of raw samples. `samples_like` is an extension of
            NumPy's array like structure. See :func:`dimod.sampleset.as_samples`.
            By default, a single sample with all +1 assignments is used. 
        current_state_energy (bool, optional, default=False): 
            By default, returns the effective field (the energy 
            contribution associated to a state assignment of 1). When set to 
            True, returns the energy lost in flipping the value of each 
            variable. Note current_state_energy is typically negative for 
            positive temperature samples, meaning energy is not decreased
            by flipping the spin against its current assignment.
    Returns:
        samples_like: 
            A Tuple of the effective_fields, and the variable labels.
            Effective fields are returned as a :obj:`numpy.ndarray`.
            Rows index samples, and columns index variables in the order
            returned by variable labels.

    Examples:
       For a ferromagnetic Ising chain :math:`H = - 0.5 \sum_i s_i s_{i+1}`
       and for a ground state sample (all +1), the energy lost when flipping
       any spin is equal to the number of couplers frustrated: -2 in the center
       of the chain (variables 1,2,..,N-2), and -1 at the end (variables 0 and N-1).

       >>> import dimod
       >>> import numpy as np
       >>> from dwave.system.temperatures import effective_field
       >>> N = 5
       >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, {(i,i+1) : -0.5 for i in range(N-1)})
       >>> var_labels = list(range(N))
       >>> samples = (np.ones(shape=(1,N)), var_labels)
       >>> E = effective_field(bqm,samples,current_state_energy=True)
       >>> print('Cost to flip spin against current assignment', E)     
       Cost to flip spin against current assignment (array([[-1., -2., -2., -2., -1.]]), [0, 1, 2, 3, 4])

    '''
    if samples is None:
        samples = np.ones(shape=(1,bqm.num_variables))
        labels = bqm.variables
    else:
        samples, labels = dimod.sampleset.as_samples(samples)

    if bqm.vartype is dimod.BINARY:
        bqm = bqm.change_vartype('SPIN', inplace=False)
        samples = 2*samples - 1

    h, (irow, icol, qdata), offset = bqm.to_numpy_vectors(
        variable_order=labels)
    # eff_field = h + J*s OR diag(Q) + (Q-diag(Q))*b
    effective_fields = np.tile(h[np.newaxis,:],(samples.shape[0],1))
    for sI in range(samples.shape[0]):
        np.add.at(effective_fields[sI,:],irow,qdata*samples[sI,icol])
        np.add.at(effective_fields[sI,:],icol,qdata*samples[sI,irow])

    if current_state_energy is True:
        #Ising: eff_field = 2*s*(h + J*s)
        effective_fields = 2*samples*effective_fields

    return (effective_fields,labels)

def maximum_pseudolikelihood_temperature(bqm=None,
                                         sampleset=None,
                                         site_energy=None,
                                         num_bootstrap_samples=0,
                                         seed=None,
                                         T_guess=None,
                                         optimize_method='bisect',
                                         T_bracket=(1e-3,1000)) -> Tuple[float,np.ndarray]:
    r'''Returns a sampling-based temperature estimate.

    The temperature T parameterizes the Boltzmann distribution as 
    :math:`P(x) = \exp(-H(x)/T)/Z(T)`, where :math:`P(x)` is a probability over a state space, 
    :math:`H(x)` is the energy function (BQM) and :math:`Z(T)` is a normalization. 
    Given a sample set (:math:`S`), a temperature estimate establishes the 
    temperature that is most likely to have produced the sample set.
    An effective temperature can be derived from a sample set by considering the
    rate of excitations only. A maximum-pseudo-likelihood (MPL) estimator 
    considers local excitations only, which are sufficient to establish a 
    temperature efficiently (in compute time and number of samples). If the BQM
    consists of uncoupled variables then the estimator is equivalent to a 
    maximum likelihood estimator.

    The effective MPL temperature is defined by the solution T to 

    .. math::
       0 = \sum_i \sum_{s \in S} f_i(s) \exp(f_i(s)/T), 

    where f is the energy lost in flipping spin i against its current 
    assignment (the effective field).

    The problem is a convex root solving problem, and is solved with SciPy 
    optimize.

    If the distribution is not Boltzmann with respect to the BQM provided, as
    may be the case for heuristic samplers (such as annealers), the temperature
    estimate can be interpreted as characterizing only a rate of local 
    excitations. In the case of sample sets obtained from D-Wave annealing 
    quantum computers the temperature can be identified with a physical 
    temperature via a late-anneal freeze-out phenomena.

    Args:
        bqm (:obj:`dimod.BinaryQuadraticModel`, optional):
            Binary quadratic model describing sample distribution.
            If ``bqm`` and ``site_energy`` are both None, then by default 
            100 samples are drawn using :class:`~dwave.system.samplers.DWaveSampler`, 
            with ``bqm`` defaulted as described.
        sampleset (:class:`~dimod.SampleSet`, optional):
            A set of samples, assumed to be fairly sampled from
            a Boltzmann distribution characterized by ``bqm``.
        site_energy (samples_like, optional):
            A Tuple of effective fields and site labels.
            Derived from the ``bqm`` and ``sampleset`` if not provided.
        num_bootstrap_samples (int, optional, default=0):
            Number of bootstrap estimators to calculate.
        seed (int, optional)
            Seeds the bootstrap method (if provided) allowing reproducibility
            of the estimators.
        T_guess (float, optional):
            User approximation to the effective temperature, must be 
            a positive scalar value.
            Seeding the root-search method can enable faster convergence.
            By default, T_guess is ignored if it falls outside the range
            of ``T_bracket``.
        optimize_method (str,optional,default='bisect'):
            SciPy method used for optimization. Options are 'bisect' and
            None (the default SciPy optimize method). 
        T_bracket (list or Tuple of 2 floats, optional, default=(0.001,1000)):
            If excitations are absent, temperature is defined as zero, otherwise 
            this defines the range of Temperatures over which to attempt a fit when
            using the 'bisect' ``optimize_method`` (the default).

    Returns:
        Tuple of float and NumPy array:
            (T_estimate,T_bootstrap_estimates)

            *T_estimate*: a temperature estimate
            *T_bootstrap_estimates*: a numpy array of bootstrap estimators

    Examples:
       Draw samples from a D-Wave Quantum Computer for a large spin-glass 
       problem (random couplers J, zero external field h).
       Establish a temperature estimate by maximum pseudo-likelihood. 

       Note that due to the complicated freeze-out properties of hard models,
       such as large scale spin-glasses, deviation from a classical Boltzmann 
       distribution is anticipated.
       Nevertheless, the T estimate can always be interpreted as an estimator
       of local excitations rates. For example T will be 0 if only 
       local minima are returned (even if some of the local minima are not 
       ground states).

       >>> import dimod
       >>> from dwave.system.temperatures import maximum_pseudolikelihood_temperature
       >>> from dwave.system import DWaveSampler
       >>> from random import random
       >>> sampler = DWaveSampler() 
       >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, {e : 1-2*random() for e in sampler.edgelist})
       >>> sampleset = sampler.sample(bqm, num_reads=100, auto_scale=False)
       >>> T,T_bootstrap =  maximum_pseudolikelihood_temperature(bqm,sampleset) 
       >>> print('Effective temperature ',T)    # doctest: +SKIP
       Effective temperature  0.24066488780293813

    See also:

        https://doi.org/10.3389/fict.2016.00023

        https://www.jstor.org/stable/25464568

    '''

    T_estimate = 0
    T_bootstrap_estimates = np.zeros(num_bootstrap_samples)

    #Check for largest local excitation in every sample, and over all samples
    if site_energy is None:
        if bqm is None or sampleset is None:
            raise ValueError('site_energy can only be derived if both'
                             'bqm and sampleset are provided as arguments')
        site_energy = effective_field(bqm,
                                      sampleset,
                                      current_state_energy = True)
    max_excitation = np.max(site_energy[0],axis=1)
    max_excitation_all =  np.max(max_excitation)
    if max_excitation_all <= 0:
        #There are no local excitations present in the sample set, therefore
        #the temperature is estimated as 0. 
        pass
    else:
        
        def d_mean_log_pseudo_likelihood(x):
            #Derivative of mean (w.r.t samples) log pseudo liklihood amounts
            #to local energy matching criteria
            #O = sum_i \sum_s f_i(s) P(s,i) #s = sample, i = variable index
            #f_i(s) is energy lost in flipping spin s_i against current assignment.
            #P(s,i) = 1/(1 + exp[x f_i(s)]), probability to flip against current state.
            #x = -1/T
            with warnings.catch_warnings():
                #Overflow errors are safe:
                warnings.simplefilter(action='ignore', category=RuntimeWarning)
                expFactor = np.exp(site_energy[0]*x)
            return np.sum(site_energy[0]/(1 + expFactor))
        
        #Ensures good gradient method, except pathological cases
        if T_guess is None:
            x0 = -1/max_excitation_all
        else:
            if T_guess < T_bracket[0]:
                x0 = -1/T_bracket[0]
            elif T_guess > T_bracket[1]:
                x0 = -1/T_bracket[1]
            else:
                x0 = -1/T_guess
        root_results = None
        if optimize_method == 'bisect':
            #Check T_bracket
            if not 0 <= T_bracket[0] < T_bracket[1]:
                raise ValueError('Bad T_bracket, must be positive ordered scalars.')
            #Convert to bisection bracket for -1/T
            bisect_bracket = [-1/T_bracket[i] for i in range(2)]
            
            if d_mean_log_pseudo_likelihood(bisect_bracket[0]) <0:
                warnings.warn(
                    'Temperature is less than T_bracket[0], or perhaps negative: ' 
                    'rescaling the Hamiltonian, modification of T_bracket[0], or '
                    'a change of the optimize_method (to None) '
                    'can resolve this issue assuming a numerical precision issue '
                    'induced by very large or small effective fields is not the cause. '
                    'Automated precision requirements mean that this routine works '
                    'best when excitations (effective fields) are O(1).')
                T_estimate = T_bracket[0]
            elif d_mean_log_pseudo_likelihood(bisect_bracket[1]) > 0:
                warnings.warn(
                    'Temperature is greater than T_bracket[1], or perhaps negative:'
                    'rescaling the Hamiltonian, modification of T_bracket[1] or '
                    'a change of the optimize_method (to None) '
                    'can resolve this issue assuming a numerical precision issue '
                    'induced by very large or small effective fields is not the cause. '
                    'Automated precision requirements mean that this routine works '
                    'best when excitations (effective fields) are O(1).')
                T_estimate = T_bracket[1]
            else:
                #Bounds are good:
                if x0 < bisect_bracket[0] or x0 > bisect_bracket[1]:
                    x0 = (bisect_bracket[0] + bisect_bracket[1])/2 
                root_results = optimize.root_scalar(f=d_mean_log_pseudo_likelihood, x0 = x0,
                                                    method=optimize_method, bracket=bisect_bracket)
                T_estimate = -1/(root_results.root)
        else:
            #For very large or small site energies this may be numerically
            #unstable, therefore bisection search is the default.
            def dd_mean_log_pseudo_likelihood(x):
                
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter(action='ignore', category=RuntimeWarning)
                    #overflow errors are harmless, +Inf is a safe value.
                    expFactor = np.exp(site_energy[0]*x)
                    #divide by zero (1/expFactor) and divide by +Inf errors are harmless
                    return np.sum(-site_energy[0]*site_energy[0]/(expFactor + 2 + 1/expFactor))

            root_results = optimize.root_scalar(f=d_mean_log_pseudo_likelihood, x0 = x0,
                                                fprime=dd_mean_log_pseudo_likelihood)
            T_estimate = -1/root_results.root
        if num_bootstrap_samples > 0:
            #By bootstrapping with respect to samples we 
            if root_results is not None:
                x0 = root_results.root
            prng = np.random.RandomState(seed)
            num_samples = site_energy[0].shape[0]
            for bs in range(num_bootstrap_samples):
                indices = np.random.choice(
                    num_samples,
                    num_bootstrap_samples,
                    replace=True)
                T_bootstrap_estimates[bs],_ = maximum_pseudolikelihood_temperature(
                    site_energy = (site_energy[0][indices,:],site_energy[1]),
                    num_bootstrap_samples = 0,
                    T_guess = T_estimate)

    return T_estimate, T_bootstrap_estimates

def Ip_in_units_of_B(Ip: Union[None, float, np.ndarray]=None,
                     B: Union[None, float, np.ndarray]=1.391,
                     MAFM: Optional[float]=1.647,
                     units_Ip: Optional[str]='uA',
                     units_B: Literal['GHz', 'J'] = 'GHz',
                     units_MAFM : Optional[str]='pH') -> Union[float, np.ndarray]:
    r"""Estimate qubit persistent current :math:`I_p(s)` in schedule units.

    Under a simple, noiseless freeze-out model, you can substitute flux biases 
    for programmed linear biases, ``h``, in the standard transverse-field Ising 
    model as implemented on D-Wave quantum computers. Perturbations in ``h`` are 
    not, however, equivalent to flux perturbations with respect to dynamics 
    because of differences in the dependence on the anneal fraction, :math:`s`: 
    :math:`I_p(s) \propto \sqrt(B(s))`. The physical origin of each term is different, 
    and so precision and noise models also differ.

    Assume a Hamiltonian in the :ref:`documented form <sysdocs_gettingstarted:doc_qpu>` 
    with an additional flux-bias-dependent component 
    :math:`H(s) \Rightarrow H(s) - H_F(s) \sum_i \Phi_i \sigma^z_i`,
    where :math:`\Phi_i` are flux biases (in units of :math:`\Phi_0`), 
    :math:`\sigma^z_i` is the Pauli-z operator, and 
    :math:`H_F(s) = Ip(s) \Phi_0`. Schedules for D-Wave quantum computers 
    specify energy in units of Joule or GHz. 

    Args:
        Ip:
            Persistent current, :math:`I_p(s)`, in units of amps or 
            microamps. When not provided, inferred from :math:`M_{AFM}` 
            and and :math:`B(s)` based on the relation 
            :math:`B(s) = 2 M_{AFM} I_p(s)^2`. 

        B:
            Annealing schedule field, :math:`B(s)`, associated with the 
            problem Hamiltonian. Schedules are provided for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            This parameter is ignored when ``Ip`` is specified.

        MAFM:
            Mutual inductance, :math:`M_{AFM}`, specified for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            ``MAFM`` is ignored when ``Ip`` is specified.

        units_Ip:
            Units in which the persistent current, ``Ip``, is specified. 
            Allowed values are ``'uA'`` (microamps) and ``'A'`` (amps)

        units_B:
            Units in which the schedule ``B`` is specified. Allowed values
            are ``'GHz'`` (gigahertz) and ``'J'`` (Joules).

        units_MAFM:
            Units in which the mutual inductance, ``MAFM``, is specified. Allowed 
            values are ``'pH'`` (picohenry) and ``'H'`` (Henry).

    Returns:
        :math:`I_p(s)` with units matching the Hamiltonian :math:`B(s)`.
    """
    h = 6.62607e-34  # Plank's constant for converting energy in Hertz to Joules 
    Phi0 = 2.0678e-15  # superconducting magnetic flux quantum (h/2e); units: Weber=J/A

    if units_B == 'GHz':
        B_multiplier = 1e9*h  # D-Wave schedules use GHz by convention
    elif units_B == 'J':
        B_multiplier = 1
    else:
        raise ValueError('Schedule B must be in units GHz or J, ' 
                         f'but given {units_B}')
    if Ip is None:
        B = B*B_multiplier # To Joules
        if units_MAFM == 'pH':
            MAFM = MAFM*1e-12  # conversion from picohenry to Henry
        elif units_MAFM != 'H':
            raise ValueError('MAFM must be in units pH or H, ' 
                             f'but given {units_MAFM}')
        Ip = np.sqrt(B/(2*MAFM))  # Units of A = C/s, O(1e-6) 
    else:
        if units_Ip == 'uA':
            Ip = Ip*1e-6  # Conversion from microamps to amp
        elif units_Ip != 'A':
            raise ValueError('Ip must be in units uA or A, ' 
                             f'but given {units_Ip}')

    return Ip*Phi0/B_multiplier


def h_to_fluxbias(h: Union[float, np.ndarray]=1,
                  Ip: Optional[float]=None,
                  B: float=1.391, MAFM: Optional[float]=1.647,
                  units_Ip: Optional[str]='uA',
                  units_B : str='GHz',
                  units_MAFM : Optional[str]='pH') -> Union[float, np.ndarray]:
    r"""Convert problem Hamiltonian bias ``h`` to equivalent flux bias.

    Unitless bias ``h`` is converted to the equivalent flux bias in units 
    :math:`\Phi_0`, the magnetic flux quantum.

    The dynamics of ``h`` and flux bias differ, as described in the
    :func:`Ip_in_units_of_B` function.
    Equivalence at a specific point in the anneal is valid under a 
    freeze-out (quasi-static) hypothesis.

    Defaults are based on the published physical properties of 
    `Leap <https://cloud.dwavesys.com/leap/>`_\ 's  
    ``Advantage_system4.1`` solver at single-qubit freezeout (:math:`s=0.612`).

    Args:
        Ip:
            Persistent current, :math:`I_p(s)`, in units of amps or 
            microamps. When not provided, inferred from :math:`M_{AFM}` 
            and and :math:`B(s)` based on the relation 
            :math:`B(s) = 2 M_{AFM} I_p(s)^2`. 

        B:
            Annealing schedule field, :math:`B(s)`, associated with the 
            problem Hamiltonian. Schedules are provided for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            This parameter is ignored when ``Ip`` is specified.

        MAFM:
            Mutual inductance, :math:`M_{AFM}`, specified for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            ``MAFM`` is ignored when ``Ip`` is specified.

        units_Ip:
            Units in which the persistent current, ``Ip``, is specified. 
            Allowed values are ``'uA'`` (microamps) and ``'A'`` (amps)

        units_B:
            Units in which the schedule ``B`` is specified. Allowed values
            are ``'GHz'`` (gigahertz) and ``'J'`` (Joules).

        units_MAFM:
            Units in which the mutual inductance, ``MAFM``, is specified. Allowed 
            values are ``'pH'`` (picohenry) and ``'H'`` (Henry).

    Returns:
        Flux-bias values producing equivalent longitudinal fields to the given 
        ``h`` values.
    """
    Ip = Ip_in_units_of_B(Ip, B, MAFM,
                          units_Ip, units_B, units_MAFM)  # Convert/Create Ip in units of B, scalar
    # B(s)/2 h_i = Ip(s) phi_i 
    return -B/2/Ip*h

def fluxbias_to_h(fluxbias: Union[float, np.ndarray]=1,
                  Ip: Optional[float]=None,
                  B: float=1.391, MAFM: Optional[float]=1.647, 
                  units_Ip: Optional[str]='uA',
                  units_B : str='GHz', units_MAFM : Optional[str]='pH') -> Union[float, np.ndarray]:
    r"""Convert flux biases to equivalent problem Hamiltonian bias ``h``.

    Converts flux biases in units of :math:`\Phi_0`, the magnetic flux quantum, 
    to equivalent problem Hamiltonian biases ``h``, which are unitless. 

    The dynamics of ``h`` and flux bias differ, as described in the
    :func:`Ip_in_units_of_B` function.
    Equivalence at a specific point in the anneal is valid under a 
    freeze-out (quasi-static) hypothesis.

    Defaults are based on the published physical properties of 
    `Leap <https://cloud.dwavesys.com/leap/>`_\ 's  
    ``Advantage_system4.1`` solver at single-qubit freezeout (:math:`s=0.612`).

    Args:
        fluxbias: 
             A flux bias in units of :math:`\Phi_0`.

        Ip:
            Persistent current, :math:`I_p(s)`, in units of amps or 
            microamps. When not provided, inferred from :math:`M_{AFM}` 
            and and :math:`B(s)` based on the relation 
            :math:`B(s) = 2 M_{AFM} I_p(s)^2`. 
    
        B:
            Annealing schedule field, :math:`B(s)`, associated with the 
            problem Hamiltonian. Schedules are provided for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            This parameter is ignored when ``Ip`` is specified.

        MAFM:
            Mutual inductance, :math:`M_{AFM}`, specified for each quantum 
            computer in the 
            :ref:`system documentation <sysdocs_gettingstarted:doc_qpu_characteristics>`. 
            ``MAFM`` is ignored when ``Ip`` is specified.

        units_Ip:
            Units in which the persistent current, ``Ip``, is specified. 
            Allowed values are ``'uA'`` (microamps) and ``'A'`` (amps)

        units_B:
            Units in which the schedule ``B`` is specified. Allowed values
            are ``'GHz'`` (gigahertz) and ``'J'`` (Joules).

        units_MAFM:
            Units in which the mutual inductance, ``MAFM``, is specified. Allowed 
            values are ``'pH'`` (picohenry) and ``'H'`` (Henry).

    Returns:
        ``h`` values producing equivalent longitudinal fields to the flux biases.
    """
    Ip = Ip_in_units_of_B(Ip, B, MAFM,
                          units_Ip, units_B, units_MAFM)  # Convert/Create Ip in units of B, scalar
    # B(s)/2 h_i = Ip(s) phi_i 
    return -2*Ip/B*fluxbias


def freezeout_effective_temperature(freezeout_B, temperature, units_B = 'GHz', units_T = 'mK') -> float:
    r'''Provides an effective temperature as a function of freezeout information.

    See https://docs.dwavesys.com/docs/latest/c_qpu_annealing.html for a 
    complete summary of D-Wave annealing quantum computer operation.

    A D-Wave annealing quantum computer is assumed to implement a Hamiltonian
    :math:`H(s) = B(s)/2 H_P - A(s)/2 H_D`, where: :math:`H_P` is the unitless 
    diagonal problem Hamiltonian,
    :math:`H_D` is the unitless driver Hamiltonian, :math:`B(s)` is the problem energy scale; A(s)
    is the driver energy scale, amd :math:`s` is the normalized anneal
    time :math:`s = t/t_a` (in [0,1]).
    Diagonal elements of :math:`H_P`, indexed by the spin state :math:`x`, are equal to
    the energy of a classical Ising spin system 

    .. math::
        E_{Ising}(x) = \sum_i h_i x_i + \sum_{i>j} J_{i,j} x_i x_j

    If annealing achieves a thermally equilibrated distribution 
    over decohered states at large :math:`s` where :math:`A(s) \ll B(s)`, 
    and dynamics stop abruptly at :math:`s=s^*`, the distribution of returned
    samples is well described by a Boltzmann distribution:

    .. math::
        P(x) = \exp(- B(s^*) R E_{Ising}(x) / 2 k_B T)

    where T is the physical temperature, and :math:`k_B` is the Boltzmann constant.
    R is a Hamiltonain rescaling factor, if a QPU is operated with auto_scale=False, 
    then R=1.
    The function calculates the unitless effective temperature as :math:`T_{eff} = 2 k_B T/B(s^*)`.

    Device temperature :math:`T`, annealing schedules {:math:`A(s)`, :math:`B(s)`} and 
    single-qubit freeze-out (:math:`s^*`, for simple uncoupled Hamltonians) are reported 
    device properties: https://docs.dwavesys.com/docs/latest/doc_physical_properties.html 
    These values (typically specified in mK and GHz) allows the calculation of an effective 
    temperature for simple Hamiltonians submitted to D-Wave quantum computers. Complicated 
    problems exploiting embeddings, or with many coupled variables, may freeze out at 
    different values of s or piecemeal). Large problems may have slow dynamics at small 
    values of s, so :math:`A(s)` cannot be ignored as a contributing factor to the distribution.

    Note that for QPU solvers this temperature estimate applies to problems
    submitted with no additional scaling factors (sampling with ``auto_scale = False``). 
    If ``auto_scale=True`` (default) additional scaling factors must be accounted for. 

    Args:
        freezeout_B (float):
             :math:`B(s^*)`, the problem Hamiltonian energy scale at freeze-out.

        temperature (float):
            :math:`T`, the physical temperature of the quantum computer.

        units_B (string, optional, 'GHz'):
            Units in which ``freezeout_B`` is specified. Allowed values:
            'GHz' (Giga-Hertz) and 'J' (Joules).

        units_T (string, optional, 'mK'):
            Units in which the ``temperature`` is specified. Allowed values:
            'mK' (milli-Kelvin) and 'K' (Kelvin).

    Returns:
        float : The effective (unitless) temperature. 

    Examples:

       This example uses the 
       `published parameters <https://docs.dwavesys.com/docs/latest/doc_physical_properties.html>`_
       for the Advantage_system4.1 QPU solver as of November 22nd 2021: 
       :math:`B(s=0.612) = 3.91` GHz , :math:`T = 15.4` mK.

       >>> from dwave.system.temperatures import freezeout_effective_temperature
       >>> T = freezeout_effective_temperature(freezeout_B = 3.91, temperature = 15.4)
       >>> print('Effective temperature at single qubit freeze-out is', T)  # doctest: +ELLIPSIS
       Effective temperature at single qubit freeze-out is 0.164...

    See also:

        The function :class:`~dwave.system.temperatures.fast_effective_temperature` 
        estimates the temperature for single-qubit Hamiltonians, in approximate
        agreement with estimates by this function at reported single-qubit 
        freeze-out values :math:`s^*` and device physical parameters.
    '''

    #Convert units_B to Joules
    if units_B == 'GHz':
        h = 6.62607e-34 #J/Hz
        freezeout_B = freezeout_B *h
        freezeout_B *= 1e9
    elif units_B == 'J':
        pass
    else:
        raise ValueException("Units must be 'J' (Joules) "
                             "or 'mK' (milli-Kelvin)")

    if units_T == 'mK':
        temperature = temperature * 1e-3
    elif units_T == 'K':
        pass
    else:
        raise ValueException("Units must be 'K' (Kelvin) "
                             "or 'mK' (milli-Kelvin)")
    kB = 1.3806503e-23 # J/K

    return 2*temperature*kB/freezeout_B

def fast_effective_temperature(sampler=None, num_reads=None, seed=None,
                               h_range=(-1/6.1,1/6.1), sampler_params=None,
                               optimize_method=None,
                               num_bootstrap_samples=0) -> Tuple[np.float64,np.float64]:
    r'''Provides an estimate to the effective temperature, :math:`T`, of a sampler.

    This function submits a set of single-qubit problems to a sampler and 
    uses the rate of excitations to infer a maximum-likelihood estimate of temperature.

    Args:
        sampler (:class:`dimod.Sampler`, optional, default=\ :class:`~dwave.system.samplers.DWaveSampler`):
            A dimod sampler. 

        num_reads (int, optional):
            Number of reads to use. Default is 100 if not specified in 
            ``sampler_params``.

        seed (int, optional):
            Seeds the problem generation process. Allowing reproducibility
            from pseudo-random samplers.

        h_range (float, optional, default = [-1/6.1,1/6.1]):
            Determines the range of external fields probed for temperature
            inference. Default is based on a D-Wave Advantage processor, where
            single-qubit freeze-out implies an effective temperature of 6.1 
            (see :class:`~dwave.system.temperatures.freezeout_effective_temperature`).
            The range should be chosen inversely proportional to the anticipated
            temperature for statistical efficiency, and to accomodate precision 
            and other nonidealities such as precision limitations.

        sampler_params (dict, optional):
            Any additional non-defaulted sampler parameterization. If 
            ``num_reads`` is a key, must be compatible with ``num_reads`` 
            argument.

        optimize_method (str, optional):
            Optimize method used by SciPy ``root_scalar`` method. The default
            method works well under default operation, 'bisect' can be 
            numerically more stable when operated without defaults.

        num_bootstrap_samples (int, optional, default=0):
            Number of bootstrap samples to use for estimation of the
            standard error. By default no bootstrapping is performed
            and the standard error is defaulted to 0.

    Returns:
        Tuple[float, float]:
            The effective temperature describing single qubit problems in an
            external field, and a standard error (+/- 1 sigma). 
            By default the confidence interval is set as 0.

    See also:

        https://doi.org/10.3389/fict.2016.00023

        https://www.jstor.org/stable/25464568

    Examples:
       Draw samples from a :class:`~dwave.system.samplers.DWaveSampler`, and establish the temperature

       >>> from dwave.system.temperatures import fast_effective_temperature
       >>> from dwave.system import DWaveSampler
       >>> sampler = DWaveSampler()
       >>> T, _ = fast_effective_temperature(sampler)
       >>> print('Effective temperature at freeze-out is',T)    # doctest: +SKIP
       0.21685104745347336

    See also:
        The function :class:`~dwave.system.temperatures.freezeout_effective_temperature` 
        may be used in combination with published device values to estimate single-qubit 
        freeze-out, in approximate agreement with empirical estimates of this function.

        https://doi.org/10.3389/fict.2016.00023

        https://www.jstor.org/stable/25464568
    '''

    if sampler is None:
        from dwave.system import DWaveSampler
        sampler = DWaveSampler()

    if 'h_range' in sampler.properties:
        if h_range[0] < sampler.properties['h_range'][0]:
            raise ValueError('h_range[0] exceeds programmable range')

        if h_range[1] > sampler.properties['h_range'][1]:
            raise ValueError('h_range[1] exceeds programmable range')

    prng = np.random.RandomState(seed)
    h_values = h_range[0] + (h_range[1]-h_range[0])*prng.rand(len(sampler.nodelist))
    bqm = dimod.BinaryQuadraticModel.from_ising({var: h_values[idx] for idx,var in enumerate(sampler.nodelist)}, {})

    #Create local sampling_params copy - default necessary additional fields:
    if sampler_params is None:
        sampler_params0 = {}
    else:
        sampler_params0 = sampler_params.copy()
    if num_reads is None:
        #Default is 100, makes efficient use of QPU access time:
        if 'num_reads' not in sampler_params0:
            sampler_params0['num_reads'] = 100 
    elif ('num_reads' in sampler_params0
        and sampler_params0['num_reads'] != num_reads):
        raise ValueError("sampler_params['num_reads'] != num_reads, "
                         "incompatible input arguments.")
    else:
        sampler_params0['num_reads'] = num_reads
    if ('auto_scale' in sampler_params0
        and sampler_params0['auto_scale'] is not False):
        raise ValueError("sampler_params['auto_scale'] == False, "
                         "is required by this method.")
    else:
        sampler_params0['auto_scale'] = False

    if num_bootstrap_samples is None:
        num_bootstrap_samples = sampler_params0['num_reads'] 

    sampleset = sampler.sample(bqm, **sampler_params0)

    T,Tboot = maximum_pseudolikelihood_temperature(
        bqm,
        sampleset,
        optimize_method=optimize_method,
        num_bootstrap_samples=num_bootstrap_samples)

    if num_bootstrap_samples == 0:
        return T, np.float64(0.0)
    else:
        return T, np.sqrt(np.var(Tboot))
