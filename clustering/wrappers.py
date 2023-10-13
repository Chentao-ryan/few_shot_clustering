from sklearn import metrics
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize


import argparse
from collections import defaultdict
import json
import matplotlib.pyplot as plt
import numpy as np
import os
import pickle
import random
from sentence_transformers import SentenceTransformer
import sys
import time
import torch

from dataloaders import load_dataset, generate_synthetic_data
from experiment_utils import set_seed, summarize_results

sys.path.extend(["..", "."])

if os.getenv("REPO_DIR") is not None:
    sys.path.append(os.path.join(os.getenv("REPO_DIR"), "clustering", "active-semi-supervised-clustering"))
else:
    sys.path.append("active-semi-supervised-clustering")
from active_semi_clustering.semi_supervised.pairwise_constraints import PCKMeans, GPTExpansionClustering
from active_semi_clustering.active.pairwise_constraints import GPT3Oracle, DistanceBasedSelector, SimilarityFinder



def LLMPairwiseClustering(features, documents, num_clusters, prompt, text_type, prompt_suffix, max_feedback_given=10000, pckmeans_w=0.4, cache_file=None, constraint_selection_algorithm="SimilarityFinder", kmeans_init="k-means++"):
    """Cluster documents using pseudo-pairwise constraints generated by an LLM.

    Args:
        features: Numpy array of features extracted for the given documents.
        documents: List of strings containing the text of each document to cluster.
        num_clusters: Number of clusters to generate.
        prompt: String for the prompt to provide to the LLM for generating pairwise
            constraints.
        text_type: Phrase describing the kind of document, used for constructing
                   prompts (e.g. "Passage", "Tweet", "Query", etc.)
        prompt_suffix: String used to append to each prompt when asking the LLM to 
            describe the relationship between two documents. This prompt will follow
            a chunk of text saying "Do <text_type> A and <text type> B"...
            Examples:
                - "discuss the same topic?"
                    (for topic-based clustering)
                - "link to the same entity on a knowledge graph like Freebase?"
                    (for entity canonicalization)
                - "describe the same sentiment, e.g. positive, negative, or neutral?"
                    (for unsupervised sentiment clustering)
        max_feedback_given: Number of pairwise constraints for the LLM to generate.
            Defaults to 10000.
        pckmeans_w: Scaling parameter for how much to weight constraint violations,
            compared to traditional embedding similarity. A higher value here will be
            more likely to respect the constraints but may override cluster assignments
            based on semantic similarity.
            Defaults to 0.4.
        cache_file: Cache file to store keyphrases generated by the LLM. Future runs
            will use these cached keyphrases when available.
        constraint_selection_algorithm: Which active constraint selection algorithm to
            use. Use "SimilarityFinder" when there is a small or moderate number of
            expected clusters relative to the number of points in the dataset. Use
            "DistanceBasedSelector" when there are a large number of expected clusters
            relative to the number of points in the dataset (e.g. <5 points expected
            per cluster).
            Defaults to "SimilarityFinder".
        kmeans_init: Initialization algorithm to use for k-means - either "k-means++"
            or "random". Defaults to "k-means++", which is usually a good choice.

    Returns:
        - Cluster IDs for each document in the provided set.
        - Pairwise constraints generated by the LLM.
    """
    oracle = GPT3Oracle(features,
                        prompt,
                        documents,
                        dataset_name=None,
                        prompt_suffix=prompt_suffix,
                        text_type=text_type,
                        cache_file=cache_file,
                        max_queries_cnt=max_feedback_given)

    print("Collecting Constraints")
    if constraint_selection_algorithm == "SimilarityFinder":
        active_learner = SimilarityFinder(n_clusters=num_clusters)
    else:
        active_learner = DistanceBasedSelector(n_clusters=num_clusters)
    active_learner.fit(features, oracle=oracle)
    pairwise_constraints = active_learner.pairwise_constraints_

    print("Training PCKMeans")
    clusterer = PCKMeans(n_clusters=num_clusters, init=kmeans_init, normalize_vectors=True, split_normalization=True, w=pckmeans_w)
    clusterer.fit(features, ml=pairwise_constraints[0], cl=pairwise_constraints[1])
    clusterer.constraints_ = pairwise_constraints
    if isinstance(oracle, GPT3Oracle) and os.path.exists(oracle.cache_file):
        oracle.cache_writer.close()
    return clusterer.labels_, clusterer.constraints_


def LLMKeyphraseClustering(features, documents, num_clusters, prompt, text_type, encoder_model=None, prompt_for_encoder=None, cache_file=None):
    """Cluster documents using their text along with keyphrases generated by an LLM.

    Args:
        features: Numpy array of features extracted for the given documents.
        documents: List of strings containing the text of each document to cluster.
        num_clusters: Number of clusters to generate.
        prompt: String for the prompt to provide to the LLM for keyphrase generation.
        text_type: Phrase describing the kind of document, used for constructing
                   prompts (e.g. "Passage", "Tweet", "Query", etc.)
        encoder_model: (Optional) encoder model object to use to encode the keyphrases
            produced by the LLM.
        prompt_for_encoder: (Optional) prompt to append to each keyphrase before
            encoding keyphrases. This is most often needed if using an
            instruction-finetuned encoder such as Instructor (Su et al, 2022).
        cache_file: Cache file to store keyphrases generated by the LLM. Future runs
            will use these cached keyphrases when available.

    Returns:
        Cluster IDs for each document in the provided set.
    """

    if encoder_model is None:
        encoder_model = SentenceTransformer('sentence-transformers/distilbert-base-nli-stsb-mean-tokens')

    clusterer = GPTExpansionClustering(features,
                                       documents,
                                       encoder_model=encoder_model,
                                       n_clusters=num_clusters,
                                       dataset_name=None,
                                       prompt=prompt,
                                       text_type=text_type,
                                       prompt_for_encoder=prompt_for_encoder,
                                       cache_file_name=cache_file,
                                       keep_original_entity=False,
                                       split=None,
                                       side_information=None,
                                       read_only=False,
                                       instruction_only=False,
                                       demonstration_only=False)
    clusterer.fit(features)
    return clusterer.labels_

