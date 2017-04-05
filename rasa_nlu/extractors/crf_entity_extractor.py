from __future__ import unicode_literals, print_function

import os
import random

import pathlib
import warnings

from typing import Optional

from rasa_nlu.components import Component
from rasa_nlu.extractors import EntityExtractor
from rasa_nlu.training_data import TrainingData
import spacy
from spacy.gold import GoldParse
import pycrfsuite
import numpy as np
from tempfile import NamedTemporaryFile
import time
import shutil
import io
import logging


class CRFEntityExtractor(Component, EntityExtractor):
    name = "ner_crf"

    context_provides = {
        "process": ["entities"],
    }

    output_provides = ["entities"]

    function_dict = {'low': lambda doc: doc[0].lower(), 'title': lambda doc: unicode(doc[0].istitle()),
                     'word3': lambda doc: doc[0][-3:], 'word2': lambda doc: doc[0][-2:],
                     'pos': lambda doc: doc[1], 'pos2': lambda doc: doc[1][:2],
                     'bias': lambda doc: u'bias', 'upper': lambda doc: unicode(doc[0].isupper()),
                     'digit': lambda doc: unicode(doc[0].isdigit())}

    def __init__(self, ent_tagger=None, crf_features=None, BILOU_flag=True):
        self.ent_tagger = ent_tagger

        # BILOU_flag determines whether to use BILOU tagging or not. More rigorous however requires more examples per entity
        # rule of thumb: use only if more than 100 egs. per entity
        self.BILOU_flag = BILOU_flag

        if not crf_features:
            # crf_features is [before, word, after] array with before, word, after holding keys about which
            # features to use for each word, for example, 'title' in array before will have the feature "is the preceeding word in title case?"
            self.crf_features = [['low', 'title', 'upper', 'pos', 'pos2'],
                                 ['bias', 'low', 'word3', 'word2', 'upper', 'title', 'digit', 'pos', 'pos2'],
                                 ['low', 'title', 'upper', 'pos', 'pos2']]
        else:
            self.crf_features = crf_features

    def train(self, training_data, spacy_nlp, BILOU_flag, crf_features):
        # type: (TrainingData) -> None
        self.BILOU_flag = BILOU_flag
        self.crf_features = crf_features
        if training_data.num_entity_examples > 0:
            train_data = self._convert_examples(training_data.entity_examples)
            ent_types = [[ent["entity"] for ent in ex["entities"]] for ex in training_data.entity_examples]
            entity_types = list(set(sum(ent_types, [])))

            # convert the dataset into features
            dataset = [self._from_json_to_crf(q, spacy_nlp) for q in train_data]
            # train the model
            self._train_model(dataset)

    def test(self, testing_data, spacy_nlp):
        if testing_data.num_entity_examples > 0:
            test_data = self._convert_examples(testing_data.entity_examples)
            ent_types = [[ent["entity"] for ent in ex["entities"]] for ex in testing_data.entity_examples]
            entity_types = list(set(sum(ent_types, [])))
            dataset = [self._from_json_to_crf(q, spacy_nlp) for q in test_data]
            self._test_model(dataset)

    def process(self, text, spacy_nlp):
        # type (unicode sentence) -> dict
        return {
            'entities': self.extract_entities(sentence, spacy_nlp)
        }

    def _convert_examples(self, entity_examples):
        def convert_entity(ent):
            return ent["start"], ent["end"], ent["entity"]

        def convert_example(ex):
            return ex["text"], [convert_entity(ent) for ent in ex["entities"]]

        return [convert_example(ex) for ex in entity_examples]

    def extract_entities(self, sentence, spacy_nlp):
        # take a sentence and return entities in json format
        # type (unicode sentence, spacy nlp) -> dict
        feature = self._from_text_to_crf(sentence, spacy_nlp)
        ents = self.ent_tagger.tag(feature)
        out_json = []
        return self._from_crf_to_json(spacy_nlp(sentence), ents)

    def _from_crf_to_json(self, sentence_doc, entities):
        # type (SpaCy Doc, array -> json)
        json_ents = []
        if len(sentence_doc) != len(entities):
            raise Exception('Inconsistency in amount of tokens between pycrfsuite and spacy')
        if self.BILOU_flag:
            # using the BILOU tagging scheme
            start_char = 0
            for word_idx in xrange(len(sentence_doc)):
                entity = entities[word_idx]
                word = sentence_doc[word_idx]
                if entity.startswith('U-'):
                    ent = {'start': start_char, 'end': start_char + len(word),
                           'value': word.text, 'entity': entity[2:]}
                    json_ents.append(ent)
                elif entity.startswith('B-'):
                    # start of a multi-word entity, need to represent whole extent
                    ent_word_idx = word_idx + 1
                    finished = False
                    end_char = start_char + len(word)
                    while not finished:
                        if entities[ent_word_idx][2:] != entity[2:]:
                            # words are not tagged the same entity class
                            logging.warn("Inconsistent BILOU tagging found, B- tag, L- tag pair encloses multiple entity classes.i.e. ['B-a','I-b','L-a'] instead of ['B-a','I-a','L-a'].\nAssuming B- class is correct.")
                        if entities[ent_word_idx].startswith('L-'):
                            # end of the entity
                            end_char += len(sentence_doc[ent_word_idx]) + 1
                            finished = True
                        elif entities[ent_word_idx].startswith('I-'):
                            # middle part of the entity
                            end_char += len(sentence_doc[ent_word_idx]) + 1
                            ent_word_idx += 1
                        else:
                            # entity not closed by an L- tag
                            finished = True
                            ent_word_idx -= 1
                            logging.warn("Inconsistent BILOU tagging found, B- tag not closed by L- tag, i.e ['B-a','I-a','O'] instead of ['B-a','L-a','O'].\nAssuming last tag is L-")
                    ent = {'start': start_char, 'end': end_char,
                           'value': sentence_doc[word_idx:ent_word_idx + 1].text,
                           'entity': entity[2:]}
                    json_ents.append(ent)
                start_char += 1 + len(word)
        elif not self.BILOU_flag:
            # not using BILOU tagging scheme, multi-word entities are split.
            start_char = 0
            for word_idx in xrange(len(sentence_doc)):
                entity = entities[word_idx]
                word = sentence_doc[word_idx]
                if entity != 'O':
                    ent = {'start': start_char, 'end': start_char + len(word),
                           'value': word.text, 'entity': entity}
                    json_ents.append(ent)
                start_char += len(word) + 1
        return json_ents

    @classmethod
    def load(cls, model_dir, model_name):
        # type: (str, str) -> CRFEntityExtractor

        if model_dir and model_name:
            ent_tagger = pycrfsuite.Tagger()
            ent_tagger.open(os.path.join(model_dir, 'ner', model_name))
            config = json.load(io.open(os.path.join(model_dir, 'ner', 'crf_config.json'), 'r'))

            return CRFEntityExtractor(ent_tagger=ent_tagger, crf_features=config['crf_features'], BILOU_flag=config['BILOU_flag'])
        else:
            return CRFEntityExtractor()

    def persist(self, model_dir):
        # type: (str) -> dict
        """Persist this model into the passed directory. Returns the metadata necessary to load the model again."""
        import json

        if self.f:
            ner_dir = os.path.join(model_dir, 'ner')
            if not os.path.exists(ner_dir):
                os.mkdir(ner_dir)

            entity_extractor_config_file = os.path.join(ner_dir, "crf_config.json")
            entity_extractor_file = os.path.join(ner_dir, "model.crfsuite")
            config = {'crf_features': self.crf_features, 'BILOU_flag': self.BILOU_flag}
            with io.open(entity_extractor_config_file, 'w') as f:
                json.dump(config, f)

            shutil.copyfileobj(self.f, io.open(entity_extractor_file, 'w'))
            return {
                "entity_extractor": "ner",
            }
        else:
            return {"entity_extractor": None}


    def _sentence_to_features(self, sentence):
        # convert a word into discrete features in self.crf_features, including word before and word after
        sentence_features = []
        for word_idx in xrange(len(sentence)):
            # word before(-1), current word(0), next word(+1)
            prefixes = [u'-1:', u'0:', u'+1:']
            word_features = []
            for i in xrange(3):
                if word_idx == len(sentence) - 1 and i == 2:
                    word_features.append('EOS')
                    # End Of Sentence
                elif word_idx == 0 and i == 0:
                    word_features.append('BOS')
                    # Beginning Of Sentence
                else:
                    word = sentence[word_idx - 1 + i]
                    prefix = prefixes[i]
                    features = self.crf_features[i]
                    for feature in features:
                        # append each feature to a feature vector
                        word_features.append(prefix + feature + ':' + self.function_dict[feature](word))
            sentence_features.append(word_features)
        return sentence_features

    def _sentence_to_labels(self, sentence):
        labels = []
        for word in sentence:
            labels.append(word[2])
        return labels

    def _from_json_to_crf(self, json_eg, spacy_nlp):
            # takes the json examples and switches them to a format which crfsuite likes
            doc = spacy_nlp(json_eg[0])
            entity_offsets = json_eg[1]
            gold = GoldParse(doc, entities=entity_offsets)
            ents = [l[5] for l in gold.orig_annot]
            if not self.BILOU_flag:
                def ent_clean(entity):
                    if entity.startswith('B-') or entity.startswith('I-') or entity.startswith('U-') or entity.startswith('L-'):
                        return entity[2:]
                    else:
                        return entity
            else:
                def ent_clean(entity): return entity

            crf_format = [(doc[i].text, doc[i].tag_, ent_clean(ents[i])) for i in xrange(len(doc))]
            return crf_format

    def _from_text_to_crf(self, sentence, spacy_nlp):
        # takes a sentence and switches it to crfsuite format
        doc = spacy_nlp(sentence)
        crf_format = [(doc[i].text, doc[i].tag_, 'N/A') for i in xrange(len(doc))]
        return crf_format

    def _train_model(self, df_train):
        # train the crf tagger based on the training data
        self.ent_tagger = pycrfsuite.Tagger()

        X_train = [self._sentence_to_features(sent) for sent in df_train]
        y_train = [self._sentence_to_labels(sent) for sent in df_train]
        trainer = pycrfsuite.Trainer(verbose=False)

        for xseq, yseq in zip(X_train, y_train):
            trainer.append(xseq, yseq)

        trainer.set_params({
            'c1': 1.0,   # coefficient for L1 penalty
            'c2': 1e-3,  # coefficient for L2 penalty
            'max_iterations': 50,  # stop earlier

            # include transitions that are possible, but not observed
            'feature.possible_transitions': True
            })
        self.f = NamedTemporaryFile()
        trainer.train(self.f.name)
        self.ent_tagger.open(self.f.name)

    def _test_model(self, df_test):

        X_test = [self._sentence_to_features(sent) for sent in df_test]
        y_test = [self._sentence_to_labels(sent) for sent in df_test]
        y_pred = [self.ent_tagger.tag(xseq) for xseq in X_test]
        print(bio_classification_report(y_test, y_pred))


def bio_classification_report(y_true, y_pred):
    # taken from https://github.com/scrapinghub/python-crfsuite/blob/master/examples/CoNLL%202002.ipynb
    # evaluates entity extraction accuracy

    """
    Classification report for a list of BIO-encoded sequences.
    It computes token-level metrics and discards "O" labels.
    Note that it requires scikit-learn 0.15+ (or a version from github master)
    to calculate averages properly!
    """
    from sklearn.preprocessing import LabelBinarizer
    from itertools import chain
    from sklearn.metrics import classification_report
    lb = LabelBinarizer()
    y_true_combined = lb.fit_transform(list(chain.from_iterable(y_true)))
    y_pred_combined = lb.transform(list(chain.from_iterable(y_pred)))

    tagset = set(lb.classes_) - {'O'}
    tagset = sorted(tagset, key=lambda tag: tag.split('-', 1)[::-1])
    class_indices = {cls: idx for idx, cls in enumerate(lb.classes_)}

    return classification_report(
        y_true_combined,
        y_pred_combined,
        labels=[class_indices[cls] for cls in tagset],
        target_names=tagset,
    )