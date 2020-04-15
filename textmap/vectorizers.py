from vectorizers import NgramVectorizer, TokenCooccurrenceVectorizer
from sklearn.base import BaseEstimator, TransformerMixin
from .transformers import (
    InformationWeightTransformer,
    RemoveEffectsTransformer,
    MultiTokenExpressionTransformer,
)
from .utilities import (
    MultiTokenCooccurrenceVectorizer,
    create_processing_pipeline_stage,
    _INFO_WEIGHT_TRANSFORERS,
    _REMOVE_EFFECT_TRANSFORMERS,
)
from .tokenizers import (
    NLTKTokenizer,
    BaseTokenizer,
    NLTKTweetTokenizer,
    SpacyTokenizer,
    StanzaTokenizer,
    SKLearnTokenizer,
)
from scipy.sparse import hstack
from sklearn.preprocessing import normalize
import pandas as pd
from sklearn.utils.validation import check_X_y, check_array, check_is_fitted


import numpy as np

# TODO: Should we wrap this in a try so we don't have a hard dependency?
# TruncatedSVD or a variety of other algorithms should also work.
from enstop import PLSA
from .utilities import flatten

_DOCUMENT_TOKENIZERS = {
    "nltk": {"class": NLTKTokenizer, "kwds": {}},
    "tweet": {"class": NLTKTweetTokenizer, "kwds": {}},
    "spacy": {"class": SpacyTokenizer, "kwds": {}},
    "stanza": {"class": StanzaTokenizer, "kwds": {}},
    "sklearn": {"class": SKLearnTokenizer, "kwds": {}},
}

_SENTENCE_TOKENIZERS = {
    "nltk": {"class": NLTKTokenizer, "kwds": {"tokenize_by": "sentence"}},
    "tweet": {"class": NLTKTweetTokenizer, "kwds": {"tokenize_by": "sentence"}},
    "spacy": {"class": SpacyTokenizer, "kwds": {"tokenize_by": "sentence"}},
    "stanza": {"class": StanzaTokenizer, "kwds": {"tokenize_by": "sentence"}},
    "sklearn": {"class": SKLearnTokenizer, "kwds": {"tokenize_by": "sentence"}},
}

_CONTRACTORS = {
    "aggressive": {
        "class": MultiTokenExpressionTransformer,
        "kwds": {"max_iterations": 6},
    },
    #"max_token_frequency": 1e-4
    "conservative": {"class": MultiTokenExpressionTransformer, "kwds": {"max_iterations": 2}},
}

_DOCUMENT_VECTORIZERS = {
    'bow': {'class': NgramVectorizer, 'kwds': {'min_frequency': 1e-5, 'excluded_token_regex': '\W+'}},
    'bigram': {'class': NgramVectorizer, 'kwds': {'ngram_size': 2, 'min_frequency': 1e-5, 'excluded_token_regex': '\W+'}}
}

# We need a few aggressive vocabulary pruning tokenizer defaults
# It's a bit unfortunate that they are buried deeply in this class.
# Maybe expose parameters at the top layer and push them down if they don't conflict.
_MULTITOKEN_COOCCURRENCE_VECTORIZERS = {
    "flat": {
        "class": MultiTokenCooccurrenceVectorizer,
        "kwds": {
            "vectorizer_list": ["before", "after"],
            "vectorizer_name_list": ["pre", "post"],
        },
    },
    "flat_1_5": {
        "class": MultiTokenCooccurrenceVectorizer,
        "kwds": {
            "vectorizer_list": ["before", "after", "before", "after"],
            "vectorizer_kwds_list": [
                {"window_args": (1,)},
                {"window_args": (1,)},
                {"window_args": (5,)},
                {"window_args": (5,)},
            ],
            "vectorizer_name_list": ["pre_1", "post_1", "pre_5", "post_5"],
        },
    },
}


class WordVectorizer(BaseEstimator, TransformerMixin):
    """
    Take a corpus of of documents and embed the words into a vector space in such a way that
    words used in similar contexts are close together.

    Parameters
    ----------
    tokenizer: string or callable (default='nltk')
        The method to be used to turn your sequence of documents into a sequence of sequences of tokens.
        If a string the options are ['nltk', 'tweet', 'spacy', 'stanza','sklearn']
    tokenizer_kwds: dict (optional, default=None)
        A dictionary of parameter names and values to pass to the tokenizer.
    token_contractor: string or callable (default='conservative')
        The method to be used to contract frequently co-occurring tokens into a single token.
        If a string the options are ['conservative', 'aggressive']
    token_contractor_kwds: dict (optional, default=None)
        A dictionary of parameter names and values to pass to the contractor.
    vectorizer: string or callable (default flat)
        The method to be used to convert the list of lists of tokens into a fixed width
        numeric representation.
    vectorizer_kwds: dict (optional, default=None)
        A dictionary of parameter names and values to pass to the vectorizer.
    normalize: bool (default=True)
        Should the output be L1 normalized?
    dedupe_sentences: bool (default=True)
        Should you remove duplicate sentences.  Repeated sentences (such as signature blocks) often
        don't provide any extra linguistic information about word usage.
    """
    def __init__(
        self,
        tokenizer="nltk",
        tokenizer_kwds=None,
        token_contractor="conservative",
        token_contractor_kwds=None,
        vectorizer="flat",
        vectorizer_kwds=None,
        normalize=True,
        dedupe_sentences=True,
    ):
        self.tokenizer = tokenizer
        self.tokenizer_kwds = tokenizer_kwds
        self.token_contractor = token_contractor
        self.token_contractor_kwds = token_contractor_kwds
        self.vectorizer = vectorizer
        self.vectorizer_kwds = vectorizer_kwds
        # Switches
        self.return_normalized = normalize
        self.dedupe_sentences = dedupe_sentences

    def fit(self, X, y=None, **fit_params):
        """
            Learns a good representation of a word as appropriately weighted count of the the
            words that it co-occurs with.  This representation also takes into account if the
            word appears before or after our work.

            Parameters
            ----------
            X: a sequence of strings
            This is typically a list of documents making up a corpus.

            Returns
            ------
            self
        """
        # TOKENIZATION
        # use tokenizer to build list of the sentences in the corpus
        # Word vectorizers are document agnostic.
        self.tokenizer_ = create_processing_pipeline_stage(
            self.tokenizer, _SENTENCE_TOKENIZERS, self.tokenizer_kwds, "tokenizer"
        )
        if self.tokenizer_ is not None:
            tokens_by_sentence = self.tokenizer_.fit_transform(X)
        else:
            tokens_by_sentence = X

        # TOKEN CONTRACTOR
        # Takes a sequence of token sequences and contracts surprisingly frequent adjacent tokens
        # into single tokens.
        self.token_contractor_ = create_processing_pipeline_stage(
            self.token_contractor,
            _CONTRACTORS,
            self.token_contractor_kwds,
            "contractor",
        )
        if self.token_contractor_ is not None:
            tokens_by_sentence = self.token_contractor_.fit_transform(
                tokens_by_sentence
            )

        # DEDUPE
        # Remove duplicate sentences.  Repeated sentences (such as signature blocks) often
        # don't provide any extra linguistic information about word usage.
        if self.dedupe_sentences:
            tokens_by_sentence = tuple(set(tokens_by_sentence))

        # VECTORIZE
        # Convert from a sequence of sequences of tokens to a sequence of fixed width numeric
        # representation.
        self.vectorizer_ = create_processing_pipeline_stage(
            self.vectorizer,
            _MULTITOKEN_COOCCURRENCE_VECTORIZERS,
            self.vectorizer_kwds,
            "MultiTokenCooccurrenceVectorizer",
        )
        self.representation_ = self.vectorizer_.fit_transform(tokens_by_sentence)

        # NORMALIZE
        if self.return_normalized:
            self.representation_ = normalize(self.representation_, norm="l1", axis=1)

        # For ease of finding we promote the token dictionary to be a full class property.
        self.token_dictonary_ = self.vectorizer_.token_dictionary_
        self.inverse_token_dictionary_ = self.vectorizer_.inverse_token_dictionary_
        self.column_dictionary_ = self.vectorizer_.column_dictionary_
        self.inverse_column_dictionary_ = self.vectorizer_.inverse_column_dictionary_
        self.vocabulary_ = self.vectorizer_.vocabulary_

        return self

    def fit_transform(self, X, y=None, **fit_params):
        """
            Learns a good representation of a word as appropriately weighted count of the the
            words that it co-occurs with.  This representation also takes into account if the
            word appears before or after our work.

            Parameters
            ----------
            X = a sequence of strings
            This is typically a list of documents making up a corpus.

            Returns
            -------
            sparse matrix
            of weighted counts of size number_of_tokens by vocabulary
            """
        self.fit(X)
        return self.representation_

    def lookup_words(self, words):
        """
        Query a model for the representations of a specific list of words.
        It ignores any words which are not contained in the model.
        Parameters
        ----------
        words=list, an iterable of the words to lookup within our model.

        Returns
        -------
        (vocabulary_present, scipy.sparse.matrix)
        A tuple with two elements.  The first is a list of the vocabulary in your words list that
        is also present in the model.
        The sparse matrix is the representations of those words
        """
        vocabulary_present = [w for w in words if w in self.vocabulary_]
        indices = [self.token_dictonary_[word] for word in vocabulary_present]
        return (vocabulary_present, self.representation_[indices, :])

    def to_DataFrame(self, max_entries=10000, words=None):
        """
        Converts the sparse matrix representation to a dense pandas DataFrame with
        one row per token and one column per token co-occurence.  This is either a
        vocabulary x vocabulary DataFrame or a vocabulary x 2*vocabulary DataFrame.
        Parameters
        ----------
        max_entries=int (10000): The maximum number of entries in a dense version of your reprsentation
            This will error if you attempt to cast to large a sparse matrix to a DataFrame
        words=iterable (None): An iterable of words to return.
            Useful for looking at a small subset of your rows.
        WARNING: this is expensive for large amounts of data since it requires the storing of zeros.
        Returns
        -------
        pandas.DataFrame
        """
        if words == None:
            words = self.vocabulary_
        vocab, submatrix = self.lookup_words(words)
        matrix_size = submatrix.shape[0] * submatrix.shape[1]
        if matrix_size > max_entries:
            return ValueError(
                f"Matrix size {matrix_size} > max_entries {max_entries}.  "
                f"Casting a sparse matrix to dense can consume large amounts of memory.  "
                f"Increase max_entries parameter in to_DataFrame() if you have enough ram "
                f"for this task. "
            )
        return pd.DataFrame(
            submatrix.todense(),
            columns=[
                self.column_dictionary_[x] for x in range(len(self.column_dictionary_))
            ],
            index=vocab,
        )


class DocVectorizer(BaseEstimator, TransformerMixin):
    def __init__(
        self,
        tokenizer="nltk",
        tokenizer_kwds=None,
        token_contractor="conservative",
        token_contractor_kwds=None,
        vectorizer="bow",
        vectorizer_kwds=None,
        info_weight_transformer="default",
        info_weight_transformer_kwds=None,
        remove_effects_transformer="default",
        remove_effects_transformer_kwds=None,
        normalize=True,
        dedupe_documents=False,
    ):
        """
        A class for converting documents into a fixed width representation.  Useful for
        comparing documents with each other.
        This is done via:
        1) Tokenization defaults to NLTK but can use stanza, spacy or a custom tokenizer.
        2) Converts this sequence of tokens into counts of n-grams (default 1-grams).
        3) Re-weights counts based on how informative the presence of an n-gram is within a document.
        4) Build a low rank model for how often we'd expect a completely random n-gram to occur your text
            and correct for this effect.

        Parameters
        ----------
        tokenizer = textmap.tokenizers.BaseTokenizer (default NLTKTokenizer)
            Takes an instantiation of a class that inherits from BaseTokenizer.
            These are classes which take documents are parse them into individual tokens,
            then optionally contract frequently co-occuring tokens together into a single
            token.
            Examples of such tokenizers can be found in textmap.tokenizers and include:
            1) NLTKTokenizer
            2) NLTKTweetTokenizer
            3) SKLearnTokenizer
            4) StanzaTokenizer
            5) SpaCyTokenizer

        ngram_vectorizer = vectorizer.NgramVectorizer (default NgramVectorizer(ngram_size=1))
            Takes an instance of a class which turns sequences of sequences of tokens into
            fixed width representation through counting the occurence of n-grams.
            In the default case this simply counts the number of occurrences of each token.
            This class returns a documents by n-gram sparse matrix of counts.

        info_weight_transformer = textmap.transformers.InformationWeightTransformer (default InformationWeightTransformer())
            Takes an instance of a class which re-weights the counts in a sparse matrix.
            It does this by building a low rank model of the probability of a word being contained
            in any document, converting that into information by applying a log and scaling our
            counts by this value.
            If this is set to None this step is skipped in the pipeline.

        remove_effect_transformer = textmap.transformer.RemoveEffectsTranformer (default RemoveEffectsTransformer())
            Takes an instance of a class which builds a low rank model for how often we'd expect a completely random word to occur your text
            and correct for this effect.
            If this is set to None this step is skipped in the pipeline.
        """
        self.tokenizer = tokenizer
        self.tokenizer_kwds = tokenizer_kwds
        self.token_contractor = token_contractor
        self.token_contractor_kwds = token_contractor_kwds
        self.vectorizer = vectorizer
        self.vectorizer_kwds = vectorizer_kwds
        self.info_weight_transformer = info_weight_transformer
        self.info_weight_transformer_kwds = info_weight_transformer_kwds
        self.remove_effects_transformer = remove_effects_transformer
        self.remove_effects_transformer_kwds = remove_effects_transformer_kwds
        # Switches
        self.normalize = normalize
        self.dedupe_documents = dedupe_documents

    def fit(self, X, y=None, **fit_params):
        """
        Learns the appropriately weighted n-gram representation of a corpus.

        Parameters
        ----------
        X = a sequence of strings
        This is typically a list of documents

        Returns
        -------
        self
        """
        # TOKENIZATION
        # use tokenizer to build list of the sentences in the corpus
        # Word vectorizers are document agnostic.
        self.tokenizer_ = create_processing_pipeline_stage(
            self.tokenizer, _DOCUMENT_TOKENIZERS, self.tokenizer_kwds, "tokenizer"
        )
        if self.tokenizer_ is not None:
            tokens_by_document = self.tokenizer_.fit_transform(X)
        else:
            tokens_by_document = X

        # TOKEN CONTRACTOR
        self.token_contractor_ = create_processing_pipeline_stage(
            self.token_contractor,
            _CONTRACTORS,
            self.token_contractor_kwds,
            "contractor",
        )
        if self.token_contractor_ is not None:
            tokens_by_document = self.token_contractor_.fit_transform(
                tokens_by_document
            )

        # DEDUPE
        #TODO: the index trick I used in UMAP unique=True

        # VECTORIZE
        self.vectorizer_ = create_processing_pipeline_stage(
            self.vectorizer,
            _DOCUMENT_VECTORIZERS,
            self.vectorizer_kwds,
            "DocumentVectorizer",
        )
        self.representation_ = self.vectorizer_.fit_transform(tokens_by_document)

        # INFO WEIGHT TRANSFORMER
        self.info_weight_transformer_ = create_processing_pipeline_stage(
            self.info_weight_transformer,
            _INFO_WEIGHT_TRANSFORERS,
            self.info_weight_transformer_kwds,
            "InformationWeightTransformer",
        )
        if self.info_weight_transformer_:
            self.representation_ = self.info_weight_transformer_.fit_transform(self.representation_)

        # REMOVE EFFECTS TRANSFORMER
        self.remove_effects_transformer_ = create_processing_pipeline_stage(
            self.remove_effects_transformer,
            _REMOVE_EFFECT_TRANSFORMERS,
            self.remove_effects_transformer_kwds,
            "RemoveEffectsTransformer",
        )
        if self.remove_effects_transformer_:
            self.representation_ = normalize(self.representation_, norm="l1", axis=1)
            self.representation_ = self.remove_effects_transformer_.fit_transform(self.representation_)

        # NORMALIZE
        if self.normalize:
            self.representation_ = normalize(self.representation_, norm="l1", axis=1)

        # For ease of finding we promote the token dictionary to be a full class property.
        self.column_dictionary_ = self.vectorizer_.inverse_ngram_dictionary_
        self.inverse_column_dictionary_ = self.vectorizer_.ngram_dictionary_
        self.vocabulary_ = list(self.vectorizer_.ngram_dictionary_.keys())

        return self

    def fit_transform(self, X, y=None, **fit_params):
        """
        Learns the appropriately weighted n-gram representation of a corpus.

        Parameters
        ----------
        X = a sequence of strings
        This is typically a list of documents

        Returns
        -------
        sparse matrix
        of weighted counts of size number_of_sequences by number of n-grams (or tokens)
        """
        self.fit(X)
        return self.representation_

    def transform(self, X):
        """
        Converts a sequence of documents into a pre-learned re-weighted weighted matrix of
        n-gram counts.

        Parameters
        ----------
        X = a sequence of strings
        This is typically a list of documents

        Returns
        -------
        sparse matrix
        of weighted counts of size number_of_sequences by number of n-grams (or tokens)

        """
        check_is_fitted(self, ["tokenizer_"])
        tokens_by_doc = self.tokenizer_.fit_transform(X)
        if self.token_contractor_ is not None:
            tokens_by_doc = self.token_contractor_.transform(tokens_by_doc)
        representation = self.vectorizer_.transform(tokens_by_doc)
        if self.info_weight_transformer_ is not None:
            representation = self.info_weight_transformer_.transform(representation)
        if self.remove_effects_transformer_ is not None:
            representation = normalize(representation, norm="l1", axis=1)
            representation = self.remove_effects_transformer_.transform(representation)
        if self.normalize:
            representation = normalize(representation, norm="l1", axis=1)
        return representation

    def to_DataFrame(self, max_entries=10000, documents=None):
        """
        Converts the sparse matrix representation to a dense pandas DataFrame with
        one row per token and one column per token co-occurence.  This is either a
        vocabulary x vocabulary DataFrame or a vocabulary x 2*vocabulary DataFrame.
        Parameters
        ----------
        max_entries: int (default=10000): The maximum number of entries in a dense version of your reprsentation
            This will error if you attempt to cast to large a sparse matrix to a DataFrame
        documents: list (optional, default=None)
            An iterable of document indices to return.
            Useful for looking at a small subset of your documents.
        WARNING: this is expensive for large amounts of data since it requires the storing of zeros.
        Returns
        -------
        pandas.DataFrame
        """
        if documents == None:
            documents = np.arange(self.representation_.shape[0])
        submatrix = self.representation_[documents, :]
        matrix_size = submatrix.shape[0] * submatrix.shape[1]
        if matrix_size > max_entries:
            return ValueError(
                f"Matrix size {matrix_size} > max_entries {max_entries}.  "
                f"Casting a sparse matrix to dense can consume large amounts of memory.  "
                f"Increase max_entries parameter in to_DataFrame() if you have enough ram "
                f"for this task. "
            )
        return pd.DataFrame(
            submatrix.todense(),
            columns=[
                self.column_dictionary_[x] for x in np.arange(submatrix.shape[1])
            ],
            index=documents,
        )



class JointVectorizer(BaseEstimator, TransformerMixin):
    def __init__(
        self,
        tokenizer=NLTKTokenizer(),
        token_contractor=MultiTokenExpressionTransformer(),
        ngram_vectorizer=NgramVectorizer(),
        info_weight_transformer=InformationWeightTransformer(),
        remove_effects_transformer=RemoveEffectsTransformer(),
        dedupe_docs_for_fit=True,
    ):
        """
        A class for converting documents into a fixed width representation.  Useful for
        comparing documents with each other.
        This is done via:
        1) Tokenization defaults to NLTK but can use stanza, spacy or a custom tokenizer.
        2) Converts this sequence of tokens into counts of n-grams (default 1-grams).
        3) Re-weights counts based on how informative the presence of an n-gram is within a document.
        4) Build a low rank model for how often we'd expect a completely random n-gram to occur your text
            and correct for this effect.

        Parameters
        ----------
        tokenizer = textmap.tokenizers.BaseTokenizer (default NLTKTokenizer)
            Takes an instantiation of a class that inherits from BaseTokenizer.
            These are classes which take documents are parse them into individual tokens,
            then optionally contract frequently co-occuring tokens together into a single
            token.
            Examples of such tokenizers can be found in textmap.tokenizers and include:
            1) NLTKTokenizer
            2) NLTKTweetTokenizer
            3) SKLearnTokenizer
            4) StanzaTokenizer
            5) SpaCyTokenizer

        ngram_vectorizer = vectorizer.NgramVectorizer (default NgramVectorizer(ngram_size=1))
            Takes an instance of a class which turns sequences of sequences of tokens into
            fixed width representation through counting the occurence of n-grams.
            In the default case this simply counts the number of occurrences of each token.
            This class returns a documents by n-gram sparse matrix of counts.

        info_weight_transformer = textmap.transformers.InformationWeightTransformer (default InformationWeightTransformer())
            Takes an instance of a class which re-weights the counts in a sparse matrix.
            It does this by building a low rank model of the probability of a word being contained
            in any document, converting that into information by applying a log and scaling our
            counts by this value.
            If this is set to None this step is skipped in the pipeline.

        remove_effect_transformer = textmap.transformer.RemoveEffectsTranformer (default RemoveEffectsTransformer())
            Takes an instance of a class which builds a low rank model for how often we'd expect a completely random word to occur your text
            and correct for this effect.
            If this is set to None this step is skipped in the pipeline.
        """

        self.tokenizer = tokenizer
        self.ngram_vectorizer = ngram_vectorizer
        # These are more minor.  I'd be willing to default them to a string to clean
        # up the docstring help.
        self.info_weight_transformer = info_weight_transformer
        self.remove_effects_transformer = remove_effects_transformer
        self.dedupe_docs_for_fit = dedupe_docs_for_fit
