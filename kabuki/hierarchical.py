 #!/usr/bin/python
from __future__ import division
from copy import copy

import numpy as np
import numpy.lib.recfunctions as rec
from ordereddict import OrderedDict

import pymc as pm
import warnings

import kabuki

class Parameter(object):
    """Specify a parameter of a model.

    :Arguments:
        name <str>: Name of parameter.

    :Optional:
        create_group_node <bool=True>: Create group nodes for parameter.
        create_subj_nodes <bool=True>: Create subj nodes for parameter.
        is_bottom_node <bool=False>: Is node at the bottom of the hierarchy (e.g. likelihoods)
        lower <float>: Lower bound (e.g. for a uniform distribution).
        upper <float>: Upper bound (e.g. for a uniform distribution).
        init <float>: Initialize to value.
        vars <dict>: User-defined variables, can be anything you later
            want to access.
        optional <bool=False>: Only create distribution when included. 
            Otherwise, set to default value (see below).
        default <float>: Default value if optional=True.
        verbose <int=0>: Verbosity.
    """

    def __init__(self, name, create_group_node=True, create_subj_nodes=True,
                 is_bottom_node=False, lower=None, upper=None, init=None,
                 vars=None, default=None, optional=False, verbose=0):
        self.name = name
        self.create_group_node = create_group_node
        self.create_subj_nodes = create_subj_nodes
        self.is_bottom_node = is_bottom_node
        self.lower = lower
        self.upper = upper
        self.init = init
        self.vars = vars
        self.optional = optional
        self.default = default
        self.verbose = verbose
        
        if self.optional and self.default is None:
            raise ValueError("Optional parameters have to have a default value.")

        self.group_nodes = OrderedDict()
        self.var_nodes = OrderedDict()
        self.subj_nodes = OrderedDict()

        # Pointers that get overwritten
        self.group = None
        self.subj = None
        self.tag = None
        self.data = None
        self.idx = None

    def reset(self):
        self.group = None
        self.subj = None
        self.tag = None
        self.data = None
        self.idx = None

    def get_full_name(self):
        if self.idx is not None:
            if self.tag is not None:
                return '%s%s%i'%(self.name, self.tag, self.idx)
            else:
                return '%s%i'%(self.name, self.idx)
        else:
            if self.tag is not None:
                return '%s%s'%(self.name, self.tag)
            else:
                return self.name

    full_name = property(get_full_name)

    def __repr__(self):
        return object.__repr__(self).replace(' object ', " '%s' "%self.name)

    
class Hierarchical(object):
    """Creation of hierarchical Bayesian models in which each subject
    has a set of parameters that are constrained by a group distribution.

    :Arguments:
        data : numpy.recarray
            Input data with a row for each trial.
            Must contain the following columns:
              * 'rt': Reaction time of trial in seconds.
              * 'response': Binary response (e.g. 0->error, 1->correct)
            May contain:
              * 'subj_idx': A unique ID (int) of the subject.
              * Other user-defined columns that can be used in depends_on
                keyword.

    :Optional:
        include : tuple
            If the model has optional arguments, they
            can be included as a tuple of strings here.

        is_group_model : bool 
            If True, this results in a hierarchical
            model with separate parameter distributions for each
            subject. The subject parameter distributions are
            themselves distributed according to a group parameter
            distribution.
        
        depends_on : dict
            Specifies which parameter depends on data
            of a column in data. For each unique element in that
            column, a separate set of parameter distributions will be
            created and applied. Multiple columns can be specified in
            a sequential container (e.g. list)

            :Example: 

            >>> depends_on={'param1':['column1']}
    
            Suppose column1 has the elements 'element1' and
            'element2', then parameters 'param1('element1',)' and
            'param1('element2',)' will be created and the
            corresponding parameter distribution and data will be
            provided to the user-specified method get_liklihood().

        trace_subjs : bool
             Save trace for subjs (needed for many
             statistics so probably a good idea.)

        plot_var : bool
             Plot group variability parameters
             (i.e. variance of Normal distribution.)

    :Note: 
        This class must be inherited. The child class must provide
        the following functions:
            * get_group_node(param): Return group mean distribution for param.
            * get_var_node(param): Return group variability distribution for param.
            * get_subj_node(param): Return subject distribution for param.
            * get_bottom_node(param, params): Return distribution
                  for nodes at the bottom of the hierarchy param (e.g. the model
                  likelihood). params contains the associated model
                  parameters.

        In addition, the variable self.params must be defined as a
        list of Paramater().

    """

    def __init__(self, data, is_group_model=None, depends_on=None, trace_subjs=True, plot_subjs=False, plot_var=False, include=()):
        # Init
        self.include = set(include)
        
        self.nodes = {}
        self.mc = None
        self.trace_subjs = trace_subjs
        self.plot_subjs = plot_subjs
        self.plot_var = plot_var

        #add data_idx field to data
        assert('data_idx' not in data.dtype.names),'A field named data_idx was found in the data file, please change it.'
        new_dtype = data.dtype.descr + [('data_idx', '<i8')]
        new_data = np.empty(data.shape, dtype=new_dtype)
        for field in data.dtype.fields:
            new_data[field] = data[field]
        new_data['data_idx'] = np.arange(len(data))
        data = new_data
        self.data = data

        if not depends_on:
            self.depends_on = {}
        else:
            # Support for supplying columns as a single string
            # -> transform to list
            for key in depends_on:
                if type(depends_on[key]) is str:
                    depends_on[key] = [depends_on[key]]
            # Check if column names exist in data        
            for depend_on in depends_on.itervalues():
                for elem in depend_on:
                    if elem not in self.data.dtype.names:
                        raise KeyError, "Column named %s not found in data." % elem
            self.depends_on = depends_on

        if is_group_model is None:
            if 'subj_idx' in data.dtype.names:
                if len(np.unique(data['subj_idx'])) != 1:
                    self.is_group_model = True
                else:
                    self.is_group_model = False
            else:
                self.is_group_model = False

        else:
            if is_group_model:
                if 'subj_idx' not in data.dtype.names:
                    raise ValueError("Group models require 'subj_idx' column in input data.")

            self.is_group_model = is_group_model

        # Should the model incorporate multiple subjects
        if self.is_group_model:
            self._subjs = np.unique(data['subj_idx'])
            self._num_subjs = self._subjs.shape[0]

            
    def _get_data_depend(self):
        """Partition data according to self.depends_on.

        :Returns:
            List of tuples with the data, the corresponding parameter
            distribution and the parameter name.

        """
        
        params = {} # use subj parameters to feed into model
        # Create new params dict and copy over nodes
        for name, param in self.params_include.iteritems():
            # Bottom nodes are created later
            if name in self.depends_on or param.is_bottom_node:
                continue
            if self.is_group_model and param.create_subj_nodes:
                params[name] = param.subj_nodes['']
            else:
                params[name] = param.group_nodes['']

        depends_on = copy(self.depends_on)

        # Make call to recursive function that does the partitioning
        data_dep = self._get_data_depend_rec(self.data, depends_on, params, [])

        return data_dep
    
    def _get_data_depend_rec(self, data, depends_on, params, dep_name, param=None):
        """Recursive function to partition data and params according
        to depends_on.

        """
        if len(depends_on) != 0: # If depends are present
            data_params = []
            # Get first param from depends_on
            param_name = depends_on.keys()[0]
            col_name = depends_on.pop(param_name) # Take out param
            depend_elements = np.unique(data[col_name])
            # Loop through unique elements
            for depend_element in depend_elements:
                # Append dependent element name.
                dep_name.append(depend_element)
                # Extract rows containing unique element
                data_dep = data[data[col_name] == depend_element]

                # Add a key that is only the col_name that links to
                # the correct dependent nodes. This is the central
                # trick so that later on the get_bottom_node can use
                # params[col_name] and the observed will get linked to
                # the correct nodes automatically.
                param = self.params_include[param_name]

                # Add the node
                if self.is_group_model and param.create_subj_nodes:
                    params[param_name] = param.subj_nodes[str(depend_element)]
                else:
                    params[param_name] = param.group_nodes[str(depend_element)]
                # Recursive call with one less dependency and the selected data.
                data_param = self._get_data_depend_rec(data_dep,
                                                       depends_on=copy(depends_on),
                                                       params=copy(params),
                                                       dep_name = copy(dep_name),
                                                       param = param)
                data_params += data_param
                # Remove last item (otherwise we would always keep
                # adding the dep elems of in one column)
                dep_name.pop()
            return data_params
                
        else: # Data does not depend on anything (anymore)
            return [(data, params, dep_name)]

    def create_nodes(self, retry=20):
        """Set group level distributions. One distribution for each
        parameter.

        :Arguments:
            retry : int
                How often to retry when model creation 
                failed (due to bad starting values).

        """
        def _create():
            for name, param in self.params_include.iteritems():
                # Bottom nodes are created elsewhere
                if param.is_bottom_node:
                    continue
                # Check if parameter depends on data
                if name in self.depends_on.keys():
                    self._set_dependent_param(param)
                else:
                    self._set_independet_param(param)

        # Include all defined parameters by default.
        self.non_optional_params = [param.name for param in self.params if not param.optional]

        # Create params dictionary
        self.params_dict = OrderedDict()
        for param in self.params:
            self.params_dict[param.name] = param
        self.params_include = OrderedDict()
        for param in self.params:
            if param.name in self.include or not param.optional:
                self.params_include[param.name] = param

        tries = 0
        while(True):
            try:
                _create()
            except (pm.ZeroProbability, ValueError) as e:
                if tries < retry:
                    tries += 1
                    continue
                else:
                    raise pm.ZeroProbability, e
            break
        
        # Init bottom nodes
        for param in self.params_include.itervalues():
            if not param.is_bottom_node:
                continue
            self._set_bottom_nodes(param, init=True)

        # Create bottom nodes
        for param in self.params_include.itervalues():
            if not param.is_bottom_node:
                continue
            self._set_bottom_nodes(param, init=False)

        # Create model dictionary
        self.nodes = {}
        for name, param in self.params_include.iteritems():
            for tag, node in param.group_nodes.iteritems():
                self.nodes[name+tag+'_group'] = node
            for tag, node in param.subj_nodes.iteritems():
                self.nodes[name+tag+'_subj'] = node
            for tag, node in param.var_nodes.iteritems():
                self.nodes[name+tag+'_var'] = node

        return self.nodes
    
    def map(self, runs=4, max_retry=4, warn_crit=5, **kwargs):
        """
        Find MAP and set optimized values to nodes.

        :Arguments:
            runs : int
                How many runs to make with different starting values
            max_retry : int
                If model creation fails, how often to retry per run
            warn_crit: float
                How far must the two best fitting values be apart in order to print a warning message

        :Returns:
            pymc.MAP object of model.

        :Note:
            Forwards additional keyword arguments to pymc.MAP().

        """

        from operator import attrgetter

        maps = []

        for i in range(runs):
            retries = 0
            # Sometimes initial values are badly chosen, so retry.
            while True:
                try:
                    # (re)create nodes to get new initival values
                    self.create_nodes()
                    m = pm.MAP(self.nodes, **kwargs)
                    m.fit()
                    maps.append(m)
                except pm.ZeroProbability as e:
                    retries += 1
                    if retries >= max_retry:
                        raise e
                    else:
                        continue
                break

        # We want to use values of the best fitting model
        sorted_maps = sorted(maps, key=attrgetter('logp'))
        max_map = sorted_maps[-1]
        
        # If maximum logp values are not in the same range, there
        # could be a problem with the model.
        if runs >= 2:
            abs_err = np.abs(sorted_maps[-1].logp - sorted_maps[-2].logp)
            if abs_err > warn_crit:
                print "Warning! Two best fitting MAP estimates are %f apart. Consider using more runs to avoid local minima." % abs_err

        # Set values of nodes
        for name, node in max_map._dict_container.iteritems():
            if not node.observed:
                self.nodes[name].value = node.value 
        
        return max_map

    def mcmc(self, *args, **kwargs):
        """
        Returns pymc.MCMC object of model.

        :Note:
            Forwards arguments to pymc.MCMC().

        """

        if not self.nodes:
            self.create_nodes()

        self.mc = pm.MCMC(self.nodes, *args, **kwargs)
        
        return self.mc

    def sample(self, *args, **kwargs):
        """Sample from posterior.
        
        :Note:
            Forwards arguments to pymc.MCMC.sample().

        """
        if not self.mc:
            self.mcmc()

        if ('hdf5' in dir(pm.database)) and \
           (type(self.mc.db) is pm.database.hdf5.Database):
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', pm.database.hdf5.tables.NaturalNameWarning)
                self.mc.sample(*args, **kwargs)
        else:
            self.mc.sample(*args, **kwargs)
        
        return self.mc

    def print_group_stats(self):
        try:
            self.stats()
            kabuki.analyze.print_group_stats(self._stats)
        except AttributeError:
            raise ValueError("No model found.")

    def print_stats(self):
        try:
            self.stats()
            kabuki.analyze.print_stats(self._stats)
        except AttributeError:
            raise ValueError("No model found.")
    

    def _set_dependent_param(self, param):
        """Set parameter that depends on data.

        :Arguments:
            param_name : string
                Name of parameter that depends on data for
                which to set distributions.

        """

        # Get column names for provided param_name
        depends_on = self.depends_on[param.name]

        # Get unique elements from the columns
        data_dep = self.data[depends_on]
        uniq_data_dep = np.unique(data_dep)

        # Loop through unique elements
        for uniq_date in uniq_data_dep:
            # Select data
            data_dep_select = self.data[(data_dep == uniq_date)]

            # Create name for parameter
            tag = str(uniq_date)

            # Create parameter distribution from factory
            param.tag = tag
            param.data = data_dep_select
            if param.create_group_node:
                param.group_nodes[tag] = self.get_group_node(param)
            else:
                param.group_nodes[tag] = None
            param.reset()

            if self.is_group_model and param.create_subj_nodes:
                # Create appropriate subj parameter
                self._set_subj_nodes(param, tag, data_dep_select)

        return self

    def _set_independet_param(self, param):
        """Set parameter that does _not_ depend on data.

        :Arguments:
            param_name : string
                Name of parameter.

        """

        # Parameter does not depend on data
        # Set group parameter
        param.tag = ''
        param.group_nodes[''] = self.get_group_node(param)
        param.reset()

        if self.is_group_model and param.create_subj_nodes:
            self._set_subj_nodes(param, '', self.data)
        
        return self

    def _set_subj_nodes(self, param, tag, data):
        """Set nodes with a parent.

        :Arguments:
            param_name : string
                Name of parameter.
            tag : string
                Element name.
            data : numpy.recarray
                Part of the data the parameter 
                depends on.

        """
        # Generate subj variability parameter var
        param.tag = 'var'+tag
        param.data = data
        param.var_nodes[tag] = self.get_var_node(param)
        param.reset()

        # Init
        param.subj_nodes[tag] = np.empty(self._num_subjs, dtype=object)
        # Create subj parameter distribution for each subject
        for subj_idx,subj in enumerate(self._subjs):
            data_subj = data[data['subj_idx']==subj]
            param.data = data_subj
            param.group = param.group_nodes[tag]
            param.var = param.var_nodes[tag]
            param.tag = tag
            param.idx = subj_idx
            param.subj_nodes[tag][subj_idx] = self.get_subj_node(param)
            param.reset()

        return self
    
    def _set_bottom_nodes(self, param, init=False):
        """Set parameter node that has no parent.

        :Arguments:
            param_name : string
                Name of parameter.
        
        :Optional:
            init : bool
                Initialize parameter.

        """
        # Divide data and parameter distributions according to self.depends_on
        data_dep = self._get_data_depend()

        # Loop through parceled data and params and create an observed stochastic
        for i, (data, params_dep, dep_name) in enumerate(data_dep):
            dep_name = str(dep_name)
            if init:
                if self.is_group_model and param.create_subj_nodes:
                    param.subj_nodes[dep_name] = np.empty(self._num_subjs, dtype=object)
                else:
                    param.subj_nodes[dep_name] = None
            else:
                self._create_bottom_node(param, data, params_dep, dep_name, i)
            
        return self
        
    def _create_bottom_node(self, param, data, params, dep_name, idx):
        """Create parameter node object which has no parent.

        :Note: 
            Called by self._set_bottom_node().

        :Arguments:
            param_name : string
                Name of parameter.
            data : numpy.recarray
                Data on which parameter depends on.
            params : list
                List of parameters the node depends on.
            dep_name : str
                Element name the node depends on.
            idx : int
                Subject index.
        
        """
        if self.is_group_model:
            for i,subj in enumerate(self._subjs):
                # Select data belonging to subj
                data_subj = data[data['subj_idx'] == subj]
                # Skip if subject was not tested on this condition
                if len(data_subj) == 0:
                    continue
                # Select params belonging to subject
                selected_subj_nodes = {}
                # Create new params dict and copy over nodes
                for selected_param in self.params_include.itervalues():
                    # Since groupless nodes are not created in this function we
                    # have to search for the correct node and include it in
                    # the params.
                    if not selected_param.create_subj_nodes:
                        if selected_param.subj_nodes.has_key(dep_name):
                            selected_subj_nodes[selected_param.name] = selected_param.group_nodes[dep_name]
                        else:
                            selected_subj_nodes[selected_param.name] = params[selected_param.name]
                    else:
                        if selected_param.subj_nodes.has_key(dep_name):
                            selected_subj_nodes[selected_param.name] = selected_param.subj_nodes[dep_name][i]
                        else:
                            selected_subj_nodes[selected_param.name] = params[selected_param.name][i]

                # Call to the user-defined function!
                param.tag = dep_name
                param.idx = i
                param.data = data_subj
                param.subj_nodes[dep_name][i] = self.get_bottom_node(param, selected_subj_nodes)
                param.reset()
        else: # Do not use subj params, but group ones
            # Since group nodes are not created in this function we
            # have to search for the correct node and include it in
            # the params
            for selected_param in self.params_include.itervalues():
                if selected_param.subj_nodes.has_key(dep_name):
                    params[selected_param.name] = selected_param.subj_nodes[dep_name]

            param.tag = dep_name
            param.data = data
            param.subj_nodes[dep_name] = self.get_bottom_node(param, params)
            param.reset()

        return self

    def compare_all_pairwise(self):
        """Perform all pairwise comparisons of dependent parameter
        distributions (as indicated by depends_on).

        :Stats generated:
            * Mean difference
            * 5th and 95th percentile

        """
        from scipy.stats import scoreatpercentile
        from itertools import combinations

        print "Parameters\tMean difference\t5%\t95%"
        # Loop through dependent parameters and generate stats
        for params in self.group_nodes_dep.itervalues():
            # Loop through all pairwise combinations
            for p0,p1 in combinations(params, 2):
                diff = self.group_nodes[p0].trace()-self.group_nodes[p1].trace()
                perc_5 = scoreatpercentile(diff, 5)
                perc_95 = scoreatpercentile(diff, 95)
                print "%s vs %s\t%.3f\t%.3f\t%.3f" %(p0, p1, np.mean(diff), perc_5, perc_95)

    def plot_all_pairwise(self):
        """Plot all pairwise posteriors to find correlations."""
        import matplotlib.pyplot as plt
        import scipy as sp
        import scipy.stats
        from itertools import combinations
        #size = int(np.ceil(np.sqrt(len(data_deps))))
        fig = plt.figure()
        fig.subplots_adjust(wspace=0.4, hspace=0.4)
        # Loop through all pairwise combinations
        for i,(p0,p1) in enumerate(combinations(self.group_nodes.values())):
            fig.add_subplot(6,6,i+1)
            plt.plot(p0.trace(), p1.trace(), '.')
            (a_s,b_s,r,tt,stderr) = sp.stats.linregress(p0.trace(), p1.trace())
            reg = sp.polyval((a_s, b_s), (np.min(p0.trace()), np.max(p0.trace())))
            plt.plot((np.min(p0.trace()), np.max(p0.trace())), reg, '-')
            plt.xlabel(p0.__name__)
            plt.ylabel(p1.__name__)
            
        plt.draw()

    def get_node(self, node_name, params):
        """Returns the node object with node_name from params if node
        is included in model, otherwise returns default value.

        """
        if node_name in self.include:
            return params[node_name]
        else:
            assert self.params_dict[node_name].default is not None, "Default value of not-included parameter not set."
            return self.params_dict[node_name].default

    def stats(self, *args, **kwargs):
        """
        smart call of MCMC.stats() for the model
        """
        try:
            nchains = self.mc.db.chains
        except AttributeError:
            raise ValueError("No model found.")
        
        #check which chain is going to be "stat"
        if 'chain' in kwargs:
            i_chain = kwargs['chain']
        else:
            i_chain = nchains
        
        #compute stats
        try:
            if self._stats_chain==i_chain:
                return self._stats
        except AttributeError:
            self._stats = self.mc.stats(*args, **kwargs)
            self._stats_chain = i_chain
            return self._stats 



    #################################
    # Methods that can be overwritten
    #################################
    def get_group_node(self, param):
        """Create and return a uniform prior distribution for group
        parameter 'param'.

        This is used for the group distributions.

        """
        return pm.Uniform(param.full_name,
                          lower=param.lower,
                          upper=param.upper,
                          value=param.init,
                          verbose=param.verbose)

    def get_var_node(self, param):
        """Create and return a Uniform prior distribution for the
        variability parameter 'param'.
        
        Note, that we chose a Uniform distribution rather than the
        more common Gamma (see Gelman 2006: "Prior distributions for
        variance parameters in hierarchical models").

        This is used for the variability fo the group distribution.

        """
        return pm.Uniform(param.full_name, lower=0., upper=10.,
                          value=.1, plot=self.plot_var)

    def get_subj_node(self, param):
        """Create and return a Truncated Normal distribution for
        'param' centered around param.group with standard deviation
        param.var and initialization value param.init.

        This is used for the individual subject distributions.

        """
        return pm.TruncatedNormal(param.full_name,
                                  a=param.lower,
                                  b=param.upper,
                                  mu=param.group, 
                                  var=param.var**-2,
                                  plot=self.plot_subjs,
                                  trace=self.trace_subjs,
                                  value=param.init)
