
import numpy as np
import pyro
import pyro.distributions as dist
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm.auto import tqdm
import warnings
from sklearn.preprocessing import scale
from scipy import sparse
from scipy.stats import fisher_exact
from scipy.sparse import isspmatrix
from mira.topic_model.base import BaseModel, get_fc_stack, logger
from pyro.contrib.autoname import scope
from pyro import poutine
from sklearn.preprocessing import scale
import mira.adata_interface.core as adi
import mira.adata_interface.regulators as ri
from mira.plots.factor_influence_plot import plot_factor_influence

class ZeroPaddedBinaryMultinomial(pyro.distributions.Multinomial):
    
    def log_prob(self, value):
        if self._validate_args:
            pass
        #self._validate_sample(value)
        logits = self.logits
        logits = logits.clone(memory_format=torch.contiguous_format)
        
        log_factorial_n = torch.lgamma((value > 0).sum(-1) + 1)
        
        logits = torch.hstack([value.new_zeros((logits.shape[0], 1)), logits])

        log_powers = torch.gather(logits, -1, value).sum(-1)
        return log_factorial_n + log_powers


class ZeroPaddedMultinomial(pyro.distributions.Multinomial):

    def log_prob(self, value):

        count, idx = value
        if self._validate_args:
            pass
        #self._validate_sample(value)
        logits = self.logits
        logits = logits.clone(memory_format=torch.contiguous_format)
        
        log_factorial_n = torch.lgamma(count.sum(-1) + 1)
        log_factorial_xs = torch.lgamma(count + 1).sum(-1)

        logits = torch.hstack([idx.new_zeros((logits.shape[0], 1)), logits])
        log_powers = torch.gather(logits, -1, idx).sum(-1)

        return log_factorial_n - log_factorial_xs + log_powers


class DANEncoder(nn.Module):

    def __init__(self, embedding_size = None, *,num_endog_features, num_topics, embedding_dropout,
        hidden, dropout, num_layers, num_exog_features, num_covariates, num_extra_features):
        super().__init__()

        if embedding_size is None:
            embedding_size = hidden

        self.word_dropout_rate = embedding_dropout
        self.embedding = nn.Embedding(num_endog_features + 1, embedding_size, padding_idx=0)
        self.num_topics = num_topics
        self.calc_readdepth = True
        self.fc_layers = get_fc_stack(
            layer_dims = [embedding_size + 1 + num_covariates + num_extra_features, 
                *[hidden]*(num_layers-2), 2*num_topics],
            dropout = dropout, skip_nonlin = True
        )

    def forward(self, idx, read_depth, covariates, extra_features):
       
        if self.training:
            corrupted_idx = torch.multiply(
                torch.empty_like(idx).bernoulli_(1-self.word_dropout_rate),
                idx
            )
        else:
            corrupted_idx = idx

        if self.calc_readdepth: # for compatibility with older models
            read_depth = (corrupted_idx > 0).sum(-1, keepdim=True)

        embeddings = self.embedding(corrupted_idx) # N, T, D
        ave_embeddings = embeddings.sum(1)/read_depth

        X = torch.hstack([ave_embeddings, read_depth.log(), covariates, extra_features]) #inject read depth into model
        X = self.fc_layers(X)

        theta_loc = X[:, :self.num_topics]
        theta_scale = F.softplus(X[:, self.num_topics:(2*self.num_topics)])  

        return theta_loc, theta_scale


    def topic_comps(self, idx, read_depth, covariates, extra_features):
        theta = self.forward(idx, read_depth, covariates, extra_features)[0]
        theta = theta.exp()/theta.exp().sum(-1, keepdim = True)
       
        return theta.detach().cpu().numpy()


class AccessibilityModel:

    encoder_model = DANEncoder
    count_model = 'binary'

    @property
    def peaks(self):
        return self.features


    def _recommend_hidden(self, n_samples):
        if n_samples <= 2000:
            return 128
        else:
            return 256

    def _recommend_embedding_size(self, n_samples):
        return None

    def _get_padded_idx_matrix(self, accessibility_matrix):

        width = int(accessibility_matrix.sum(-1).max())

        dense_matrix = []
        for i in range(accessibility_matrix.shape[0]):

            if self.count_model == 'binary':
                row = accessibility_matrix[i,:].indices + 1
            else:
                row = np.repeat(accessibility_matrix[i,:].indices, accessibility_matrix[i,:].data.astype(int)) + 1

            if len(row) == width:
                dense_matrix.append(np.array(row)[np.newaxis, :])
            else:
                dense_matrix.append(np.concatenate([np.array(row), np.zeros(width - len(row))])[np.newaxis, :]) #0-pad tail to "width"

        dense_matrix = np.vstack(dense_matrix)
        
        return dense_matrix


    def _dense_counts_matrix(self, accessibility_matrix):

        width = int((accessibility_matrix > 0).sum(-1).max())

        dense_matrix = []
        for i in range(accessibility_matrix.shape[0]):
            row = accessibility_matrix[i,:].data
            if len(row) == width:
                dense_matrix.append(np.array(row)[np.newaxis, :])
            else:
                dense_matrix.append(np.concatenate([np.array(row), np.zeros(width - len(row))])[np.newaxis, :]) #0-pad tail to "width"

        dense_matrix = np.vstack(dense_matrix)
        
        return dense_matrix

    @staticmethod
    def _binarize_matrix(X):
        assert(isinstance(X, np.ndarray) or isspmatrix(X))
        
        if not isspmatrix(X):
            X = sparse.csr_matrix(X)

        assert(len(X.shape) == 2)
        
        assert(np.isclose(X.data.astype(np.uint16), X.data, 1e-2).all()), 'Input data must be raw transcript counts, represented as integers. Provided data contains non-integer values.'

        X.data = np.ones_like(X.data)

        return X


    ### OLD CODE ###
    '''def _get_padded_idx_matrix(self, accessibility_matrix):

        width = int(accessibility_matrix.sum(-1).max())

        dense_matrix = []
        for i in range(accessibility_matrix.shape[0]):
            row = accessibility_matrix[i,:].indices + 1
            if len(row) == width:
                dense_matrix.append(np.array(row)[np.newaxis, :])
            else:
                dense_matrix.append(np.concatenate([np.array(row), np.zeros(width - len(row))])[np.newaxis, :]) #0-pad tail to "width"

        dense_matrix = np.vstack(dense_matrix)
        
        return dense_matrix


    def get_endog_fn(self):

        def preprocess_endog(X):
        
            return self._get_padded_idx_matrix(
                    self._binarize_matrix(X, self.num_endog_features)).astype(np.int32)

        return preprocess_endog
                   

    def get_exog_fn(self):
        
        def preprocess_exog(X):

            return self._get_padded_idx_matrix(
                    self._binarize_matrix(X, self.num_exog_features)
                    ).astype(np.int64)

        return preprocess_exog'''


    def preprocess_endog(self, X):
        
        return self._get_padded_idx_matrix(
                self._binarize_matrix(X)
                ).astype(np.int32)

    def preprocess_exog(self, X):

        return self._get_padded_idx_matrix(
                self._binarize_matrix(X)
            ).astype(np.int64)

    '''def get_rd_fn(self):
        
        def preprocess_read_depth(X):
            return np.array((X > 0).sum(-1)).reshape((-1,1)).astype(np.float32)
        
        return preprocess_read_depth


    def get_endog_fn(self):

        def preprocess_endog_binary(X):
        
            return self._get_padded_idx_matrix(
                    self._binarize_matrix(X, self.num_endog_features)).astype(np.int32)

        def preprocess_endog(X):
        
            return self._get_padded_idx_matrix(X).astype(np.int64)

        if self.count_model == 'binary':
            return preprocess_endog_binary

        return preprocess_endog
                   

    def get_exog_fn(self):
        
        def preprocess_exog(X):

            return self._dense_counts_matrix(X).astype(np.int64)

        def preprocess_exog_binary(X):

            return self._get_padded_idx_matrix(
                    self._binarize_matrix(X, self.num_exog_features)
                    ).astype(np.int64)

        if self.count_model == 'binary':
            return preprocess_exog_binary

        return preprocess_exog'''

    def suggest_parameters(self, tuner, trial):

        params = dict(        
            num_topics = trial.suggest_int('num_topics', tuner.min_topics, 
                tuner.max_topics, log=True),
        )

        if tuner.rigor >= 1:
            
            params.update(
                dict(
                    decoder_dropout = trial.suggest_float('decoder_dropout', 0.05, 0.2, log = True),
                )
            )

        if tuner.rigor >= 2:

            ## kitchen sink strategy
            params.update(dict(
                encoder_dropout = trial.suggest_float('encoder_dropout', 0.0001, 0.1, log = True),
                num_layers = trial.suggest_categorical('num_layers', (2,3,)),
                max_momentum = trial.suggest_float('max_momentum', 0.90, 0.98, log = True),
                min_momentum = trial.suggest_float('min_momentum', 0.8, 0.89, log = True),
                weight_decay = trial.suggest_float('weight_decay', 0.00001, 0.1, log = True)
            ))

        return params


    def _argsort_peaks(self, topic_num):
        assert(isinstance(topic_num, int) and topic_num < self.num_topics and topic_num >= 0)
        return np.argsort(self._score_features()[topic_num, :])


    def rank_peaks(self, topic_num):
        return self.peaks[self._argsort_peaks(topic_num)]


    def _validate_hits_matrix(self, hits_matrix):
        assert(isspmatrix(hits_matrix))
        assert(len(hits_matrix.shape) == 2)
        assert(hits_matrix.shape[1] == len(self.peaks))
        hits_matrix = hits_matrix.tocsr()

        hits_matrix.data = np.ones_like(hits_matrix.data)
        return hits_matrix
    

    @adi.wraps_modelfunc(ri.fetch_factor_hits, adi.return_output,
        ['hits_matrix','metadata'])
    def get_enriched_TFs(self, factor_type = 'motifs', top_quantile = 0.2, *, 
            topic_num, hits_matrix, metadata):
        '''
        Get TF enrichments in top peaks associated with a topic. Can be used to
        associate a topic with either motif or ChIP hits from Cistrome's 
        collection of public ChIP-seq data.

        Before running this function, one must run either:
        `mira.tl.get_motif_hits_in_peaks`

        or:
        `mira.tl.get_ChIP_hits_in_peaks`

        Parameters
        ----------
        factor_type : str, 'motifs' or 'chip', default = 'motifs'
            Which factor type to use for enrichment
        top_quantile : float > 0, default = 0.2
            Top quantile of peaks to use to represent topic in fisher exact test.
        topic_num : int > 0
            Topic for which to get enrichments
        
        Examples
        --------

        .. code-block:: python

            >>> mira.tl.get_motif_hits_in_peaks(atac_data, genome_fasta = '~/genome.fa')
            >>> atac_model.get_enriched_TFs(atac_data, topic_num = 10)

        '''

        assert(isinstance(top_quantile, float) and top_quantile > 0 and top_quantile < 1)
        hits_matrix = self._validate_hits_matrix(hits_matrix)

        module_idx = self._argsort_peaks(topic_num)[-int(self.num_exog_features*top_quantile) : ]

        pvals, test_statistics = [], []
        for i in tqdm(range(hits_matrix.shape[0]), 'Finding enrichments'):

            tf_hits = hits_matrix[i,:].indices
            overlap = len(np.intersect1d(tf_hits, module_idx))
            module_only = len(module_idx) - overlap
            tf_only = len(tf_hits) - overlap
            neither = self.num_exog_features - (overlap + module_only + tf_only)

            contingency_matrix = np.array([[overlap, module_only], [tf_only, neither]])
            stat,pval = fisher_exact(contingency_matrix, alternative='greater')
            pvals.append(pval)
            test_statistics.append(stat)

        results = [
            dict(**meta, pval = pval, test_statistic = test_stat)
            for meta, pval, test_stat in zip(metadata, pvals, test_statistics)
        ]

        self.enrichments[(factor_type, topic_num)] = results


    @adi.wraps_modelfunc(ri.fetch_factor_hits_and_latent_comps, ri.make_motif_score_adata,
        ['metadata','hits_matrix','topic_compositions'])
    def get_motif_scores(self, batch_size=512,*, metadata, hits_matrix, topic_compositions):
        '''
        Get motif scores for each cell based on the probability of sampling a motif
        from the posterior distribution over accessible sites in a cell.

        Parameters
        ----------

        adata : anndata.AnnData
            AnnData of accessibility features, annotated with TF binding using
            mira.tl.get_motif_hits_in_peaks or mira.tl.get_ChIP_hits_in_peaks.
        batch_size : int>0, default=512
            Minibatch size to calculate posterior distribution over accessible
            regions. Only affects the amount of memory used.
        factor_type : str, 'motifs' or 'chip', default = 'motifs'
            Which factor type to use for enrichment.

        Returns
        -------

        motif_scores : anndata.AnnData of shape (n_cells, n_factors)
            AnnData object. For each cell from the original adata, gives
            score for each motif/ChIP factor. 

        '''

        hits_matrix = self._validate_hits_matrix(hits_matrix)
    
        motif_scores = np.vstack([
            hits_matrix.dot(np.log(peak_probabilities).T).T
            for peak_probabilities in self._batched_impute(topic_compositions)
        ])

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            normalized_scores = scale(motif_scores/np.linalg.norm(motif_scores, axis=-1, keepdims=True))

        return metadata, motif_scores, normalized_scores


    def get_enrichments(self, topic_num, factor_type = 'motifs'):
        '''
        Returns TF enrichments for a certain topic.

        Parameters
        ----------
        topic_num : int
            For which topic to return results
        factor_type : str, 'motifs' or 'chip', default = 'motifs'
            Which factor type to use for enrichment

        Returns
        -------
        
        topic_enrichments : list[dict]
            For each record, gives a dict of 
            {'factor_id' : <id>,
            'name' : <name>,
            'parsed_name' : <name used for expression lookup>,
            'pval' : <pval>,
            'test_statistic' : <statistic>}

        Raises
        ------

        KeyError : if *get_enriched_TFs* was not yet run for the given topic.

        '''
        try:
            return self.enrichments[(factor_type, topic_num)]
        except KeyError:
            raise KeyError('User has not gotten enrichments yet for topic {} using factor_type: {}. Run "get_enriched_TFs" function.'\
                .format(str(topic_num), str(factor_type)))


    def plot_compare_topic_enrichments(self, topic_1, topic_2, factor_type = 'motifs', 
        label_factors = None, hue = None, palette = 'coolwarm', hue_order = None, 
        ax = None, figsize = (8,8), legend_label = '', show_legend = True, fontsize = 13, 
        pval_threshold = (1e-50, 1e-50), na_color = 'lightgrey',
        color = 'grey', label_closeness = 3, max_label_repeats = 3, show_factor_ids = False):
        '''
        It is often useful to contrast topic enrichments in order to
        understand which factors' influence is unique to certain
        cell states. Topics may be enriched for constitutively-active
        transcription factors, so comparing two similar topics to find
        the factors that are unique to each elucidates the dynamic
        aspects of regulation between states.

        This function contrasts the enrichments of two topics.

        Parameters
        ----------

        topic1, topic2 : int
            Which topics to compare.
        factor_type : str, 'motifs' or 'chip', default = 'motifs'
            Which factor type to use for enrichment.
        label_factors : list[str], np.ndarray[str], None; default=None
            List of factors to label. If not provided, will label all
            factors that meet the p-value thresholds.
        hue : dict[str : {str, float}] or None
            If provided, colors the factors on the plot. The keys of the dict
            must be the names of transcription factors, and the values are
            the associated data to map to colors. The values may be 
            categorical, e.g. cluster labels, or scalar, e.g. expression
            values. TFs not provided in the dict are colored as *na_color*.
        palette : str, list[str], or None; default = None
            Palette of plot. Default of None will set `palette` to the style-specific default.
        hue_order : list[str] or None, default = None
            Order to assign hues to features provided by `data`. Works similarly to
            hue_order in seaborn. User must provide list of features corresponding to 
            the order of hue assignment. 
        ax : matplotlib.pyplot.axes, deafult = None
            Provide axes object to function to add streamplot to a subplot composition,
            et cetera. If no axes are provided, they are created internally.
        figsize : tuple(float, float), default = (8,8)
            Size of figure
        legend_label : str, None
            Label for legend.
        show_legend : boolean, default=True
            Show figure legend.
        fontsize : int>0, default=13
            Fontsize of TF labels on plot.
        pval_threshold : tuple[float, float], default=(1e-50, 1e-50)
            Threshold below with TFs will not be labeled on plot. The first and
            second positions relate p-value with respect to topic 1 and topic 2.
        na_color : str, default='lightgrey'
            Color for TFs with no provided *hue*
        color : str, default='grey'
            If *hue* not provided, colors all points on plot this color.
        label_closeness : int>0, default=3
            Closeness of TF labels to points on plot. When *label_closeness* is high,
            labels are forced to be very close to points.
        max_label_repeats : boolean, default=3
            Some TFs have multiple ChIP samples or Motif PWMs. For these factors,
            label the top *max_label_repeats* examples. This prevents clutter when
            many samples for the same TF are close together. The rank of the sample
            for each TF is shown in the label as "<TF name> (<rank>)".

        Returns
        -------

        matplotlib.pyplot.axes

        Examples
        --------

        .. code-block :: python

            >>> label = ['LEF1','HOXC13','MEOX2','DLX3','BACH2','RUNX1', 'SMAD2::SMAD3']
            >>> atac_model.plot_compare_topic_enrichments(23, 17,
            ...     label_factors = label, 
            ...     color = 'lightgrey',
            ...     fontsize=20, label_closeness=5, 
            ... )

        .. image:: /_static/mira.topics.AccessibilityModel.plot_compare_topic_enrichments.svg
            :width: 300

        '''

        m1 = self.get_enrichments(topic_1, factor_type)
        m2 = self.get_enrichments(topic_2, factor_type)        
        
        return plot_factor_influence(m1, m2, ax = ax, label_factors = label_factors,
            pval_threshold = pval_threshold, hue = hue, hue_order = hue_order, 
            palette = palette, legend_label = legend_label, show_legend = show_legend, label_closeness = label_closeness, 
            na_color = na_color, max_label_repeats = max_label_repeats, figsize=figsize,
            axlabels = ('Topic {} Enrichments'.format(str(topic_1)),'Todule {} Enrichments'.format(str(topic_2))), 
            fontsize = fontsize, color = color)
