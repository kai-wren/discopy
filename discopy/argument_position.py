import logging
import os
import pickle
import ujson as json

import nltk
from nltk.tree import ParentedTree
from sklearn.ensemble import BaggingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_selection import SelectKBest, VarianceThreshold, mutual_info_classif
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import precision_recall_fscore_support, accuracy_score, cohen_kappa_score
from sklearn.pipeline import Pipeline

import discopy.conn_head_mapper
from discopy.features import get_connective_sentence_position, lca, get_pos_features
from discopy.utils import init_logger

logger = logging.getLogger('discopy')

lemmatizer = nltk.stem.WordNetLemmatizer()


def get_features(ptree: ParentedTree, connective: str, leaf_index: list):
    chm = discopy.conn_head_mapper.ConnHeadMapper()
    head, connective_head_index = chm.map_raw_connective(connective)
    connective_head_index = [leaf_index[i] for i in connective_head_index]

    lca_loc = lca(ptree, leaf_index)
    conn_tag = ptree[lca_loc].label()
    conn_pos_relative = get_connective_sentence_position(connective_head_index, ptree)

    prev, prev_conn, prev_pos, prev_pos_conn_pos = get_pos_features(ptree, leaf_index, head, -1)
    prev2, prev2_conn, prev2_pos, prev2_pos_conn_pos = get_pos_features(ptree, leaf_index, head, -2)

    prev = lemmatizer.lemmatize(prev)
    prev2 = lemmatizer.lemmatize(prev2)

    feat = {'connective': head, 'connectivePOS': conn_tag, 'cPosition': conn_pos_relative, 'prevWord+c': prev_conn,
            'prevPOSTag': prev_pos, 'prevPOS+cPOS': prev_pos_conn_pos, 'prevWord': prev, 'prev2Word+c': prev2_conn,
            'prev2POSTag': prev2_pos, 'prev2POS+cPOS': prev2_pos_conn_pos, 'prevWord2': prev2}

    return feat


def generate_pdtb_features(pdtb, parses):
    features = []
    for relation in filter(lambda i: i['Type'] == 'Explicit', pdtb):
        doc = relation['DocID']
        connective = relation['Connective']['TokenList']
        connective_raw = relation['Connective']['RawText']
        leaf_indices = [token[4] for token in connective]
        ptree = parses[doc]['sentences'][connective[0][3]]['parsetree']
        try:
            ptree = nltk.ParentedTree.fromstring(ptree)
        except ValueError:
            continue
        if not ptree.leaves():
            continue

        arg1 = sorted({i[3] for i in relation['Arg1']['TokenList']})
        arg2 = sorted({i[3] for i in relation['Arg2']['TokenList']})
        if not arg1 or not arg2:
            continue
        if arg1[-1] < arg2[0]:
            features.append((get_features(ptree, connective_raw, leaf_indices), 'PS'))
        elif len(arg1) == 1 and len(arg2) == 1 and arg1[0] == arg2[0]:
            features.append((get_features(ptree, connective_raw, leaf_indices), 'SS'))
    return list(zip(*features))


class ArgumentPositionClassifier:
    def __init__(self, n_estimators=1):
        if n_estimators > 1:
            self.model = Pipeline([
                ('vectorizer', DictVectorizer()),
                ('variance', VarianceThreshold(threshold=0.001)),
                ('selector', SelectKBest(mutual_info_classif, k=100)),
                ('model', BaggingClassifier(
                    base_estimator=SGDClassifier(loss='log', penalty='l2', average=32, tol=1e-3, max_iter=100,
                                                 n_jobs=-1, class_weight='balanced', random_state=0),
                    n_estimators=n_estimators, max_samples=0.75, n_jobs=-1))
            ])
        else:
            self.model = Pipeline([
                ('vectorizer', DictVectorizer()),
                ('variance', VarianceThreshold(threshold=0.001)),
                ('selector', SelectKBest(mutual_info_classif, k=100)),
                ('model',
                 SGDClassifier(loss='log', penalty='l2', average=32, tol=1e-3, max_iter=100, n_jobs=-1,
                               class_weight='balanced', random_state=0))
            ])

    def load(self, path):
        self.model = pickle.load(open(os.path.join(path, 'position_clf.p'), 'rb'))

    def save(self, path):
        pickle.dump(self.model, open(os.path.join(path, 'position_clf.p'), 'wb'))

    def fit(self, pdtb, parses):
        X, y = generate_pdtb_features(pdtb, parses)
        self.model.fit(X, y)

    def score_on_features(self, X, y):
        y_pred = self.model.predict_proba(X)
        y_pred_c = self.model.classes_[y_pred.argmax(axis=1)]
        logger.info("Evaluation: ArgPos")
        logger.info("    Acc  : {:<06.4}".format(accuracy_score(y, y_pred_c)))
        prec, recall, f1, support = precision_recall_fscore_support(y, y_pred_c, average='macro')
        logger.info("    Macro: P {:<06.4} R {:<06.4} F1 {:<06.4}".format(prec, recall, f1))
        logger.info("    Kappa: {:<06.4}".format(cohen_kappa_score(y, y_pred_c)))

    def score(self, pdtb, parses):
        X, y = generate_pdtb_features(pdtb, parses)
        self.score_on_features(X, y)

    def get_argument_position(self, parse, connective: str, leaf_index):
        x = get_features(parse, connective, leaf_index)
        probs = self.model.predict_proba([x])[0]
        return self.model.classes_[probs.argmax()], probs.max()


if __name__ == "__main__":
    logger = init_logger()

    pdtb_train = [json.loads(s) for s in
                  open('/data/discourse/conll2016/en.train/relations.json', 'r').readlines()]
    parses_train = json.loads(open('/data/discourse/conll2016/en.train/parses.json').read())
    pdtb_val = [json.loads(s) for s in open('/data/discourse/conll2016/en.test/relations.json', 'r').readlines()]
    parses_val = json.loads(open('/data/discourse/conll2016/en.test/parses.json').read())

    clf = ArgumentPositionClassifier()
    logger.info('Train model')
    clf.fit(pdtb_train, parses_train)
    logger.info('Evaluation on TRAIN')
    clf.score(pdtb_train, parses_train)
    logger.info('Evaluation on TEST')
    clf.score(pdtb_val, parses_val)
    clf.save('../tmp')
