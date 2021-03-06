""" Translation main class """
from __future__ import unicode_literals, print_function

import torch
from onmt.inputters.text_dataset import TextMultiField
from onmt.inputters.lattice_dataset import LatticeMultiField
from onmt.utils.alignment import build_align_pharaoh


class TranslationBuilder(object):
    """
    Build a word-based translation from the batch output
    of translator and the underlying dictionaries.

    Replacement based on "Addressing the Rare Word
    Problem in Neural Machine Translation" :cite:`Luong2015b`

    Args:
       data (onmt.inputters.Dataset): Data.
       fields (List[Tuple[str, torchtext.data.Field]]): data fields
       n_best (int): number of translations produced
       replace_unk (bool): replace unknown words using attention
       has_tgt (bool): will the batch have gold targets
    """

    def __init__(self, data, fields, n_best=1, replace_unk=False,
                 has_tgt=False, phrase_table=""):
        self.data = data
        self.fields = fields
        self._has_text_ans = isinstance(
            dict(self.fields)["ans"], TextMultiField)
        self._has_lattice_ques = isinstance(
            dict(self.fields)["ques"], LatticeMultiField)
        self.n_best = n_best
        self.replace_unk = replace_unk
        self.phrase_table = phrase_table
        self.has_tgt = has_tgt

    def _build_target_tokens(self, ques, ans, self_attention, src_vocab, ques_raw, ans_raw, pred, attn):
        tgt_field = dict(self.fields)["tgt"].base_field
        vocab = tgt_field.vocab
        tokens = []
        for tok in pred:
            if tok < len(vocab):
                tokens.append(vocab.itos[tok])
            else:
                tokens.append(src_vocab.itos[tok - len(vocab)])
            if tokens[-1] == tgt_field.eos_token:
                tokens = tokens[:-1]
                break
        if self.replace_unk and attn is not None and ques is not None and ans is not None:
            for i in range(len(tokens)):
                if tokens[i] == tgt_field.unk_token:
                    len_src_raw = len(ques_raw)+len(ans_raw)
                    _, max_index = attn[i][:len_src_raw].max(0)
                    #########################
                    if max_index < len(ques_raw):
                        ###########33 need to change for confnet self-attention ##################

                        par_arcs = ques_raw[max_index.item()]
                        _, max_par_arc_index = self_attention[max_index.item(), :len(par_arcs)].max(0)
                        tokens[i] = par_arcs[max_par_arc_index]
                    else:
                        tokens[i] = ans_raw[max_index.item()-len(ques_raw)]
                    #tokens[i] = src_raw[max_index.item()]
                    ###############################
                    if self.phrase_table != "":
                        src_raw = ques_raw + [[w] for w in ans_raw]
                        with open(self.phrase_table, "r") as f:
                            for line in f:
                                if line.startswith(src_raw[max_index.item()][0]):
                                    tokens[i] = line.split('|||')[1].strip()
        return tokens

    def from_batch(self, translation_batch):
        batch = translation_batch["batch"]
        assert(len(translation_batch["gold_score"]) ==
               len(translation_batch["predictions"]))
        batch_size = batch.batch_size

        self_attention = translation_batch["self_attention"]
        preds, pred_score, attn, align, gold_score, indices = list(zip(
            *sorted(zip(translation_batch["predictions"],
                        translation_batch["scores"],
                        translation_batch["attention"],
                        #translation_batch["self_attention"],
                        translation_batch["alignment"],
                        translation_batch["gold_score"],
                        batch.indices.data),
                    key=lambda x: x[-1])))

        if not any(align):  # when align is a empty nested list
            align = [None] * batch_size

        # Sorting
        inds, perm = torch.sort(batch.indices)
        if self._has_text_ans:
            #print('ans size', batch.ans[0].size())
            ans = batch.ans[0][:, :, 0].index_select(1, perm)
            ques = batch.ques[0][:, :, :, 0].index_select(0, perm)
        else:
            ques = None
            ans = None
        tgt = batch.tgt[:, :, 0].index_select(1, perm) \
            if self.has_tgt else None

        translations = []
        for b in range(batch_size):
            if self._has_text_ans:
                src_vocab = self.data.src_vocabs[inds[b]] \
                    if self.data.src_vocabs else None
                sattention = self_attention[:, b, :]
                ans_raw = self.data.examples[inds[b]].ans[0]
                ques_raw = self.data.examples[inds[b]].ques[0]
            else:
                src_vocab = None
                ques_raw = None
                ans_raw = None
                sattention = self_attention[:, b, :]
            pred_sents = [self._build_target_tokens(
                ques[b, :, :] if ques is not None else None,
                ans[:, b] if ans is not None else None,
                sattention,
                src_vocab, ques_raw, ans_raw,
                preds[b][n], attn[b][n])
                for n in range(self.n_best)]
            print('ques', [par_arcs[0] for par_arcs in ques_raw if par_arcs[0] != '*DELETE*' and par_arcs[0] != '[noise]'])
            print('ans', [w for w in ans_raw if w != '<blank>'])
            print('pred_sent', pred_sents)
            gold_sent = None
            if tgt is not None:
                gold_sent = self._build_target_tokens(
                    ques[b, :, :] if ques is not None else None,
                    ans[:, b] if ans is not None else None,
                    sattention,
                    src_vocab, ques_raw, ans_raw,
                    tgt[1:, b] if tgt is not None else None, None)
            print('gold_sent', gold_sent)
            print('******************')
            translation = Translation(
                ques[b, :, :] if ques is not None else None,
                ans[:, b] if ans is not None else None,
                ques_raw, ans_raw, pred_sents, attn[b], pred_score[b],
                gold_sent, gold_score[b], align[b]
            )
            translations.append(translation)

        return translations


class Translation(object):
    """Container for a translated sentence.

    Attributes:
        src (LongTensor): Source word IDs.
        src_raw (List[str]): Raw source words.
        pred_sents (List[List[str]]): Words from the n-best translations.
        pred_scores (List[List[float]]): Log-probs of n-best translations.
        attns (List[FloatTensor]) : Attention distribution for each
            translation.
        gold_sent (List[str]): Words from gold translation.
        gold_score (List[float]): Log-prob of gold translation.
        word_aligns (List[FloatTensor]): Words Alignment distribution for
            each translation.
    """

    __slots__ = ["ques", "ans", "ques_raw", "ans_raw", "pred_sents", "attns", "pred_scores",
                 "gold_sent", "gold_score", "word_aligns"]

    def __init__(self, ques, ans, ques_raw, ans_raw, pred_sents,
                 attn, pred_scores, tgt_sent, gold_score, word_aligns):
        self.ques = ques
        self.ans = ans
        self.ques_raw = ques_raw
        self.ans_raw = ans_raw
        self.pred_sents = pred_sents
        self.attns = attn
        self.pred_scores = pred_scores
        self.gold_sent = tgt_sent
        self.gold_score = gold_score
        self.word_aligns = word_aligns

    def log(self, sent_number):
        """
        Log translation.
        """

        msg = ['\nSENT {}: {}\n {}\n**************'.format(sent_number, self.ques_raw, self.ans_raw)]

        best_pred = self.pred_sents[0]
        best_score = self.pred_scores[0]
        pred_sent = ' '.join(best_pred)
        msg.append('PRED {}: {}\n'.format(sent_number, pred_sent))
        msg.append("PRED SCORE: {:.4f}\n".format(best_score))

        if self.word_aligns is not None:
            pred_align = self.word_aligns[0]
            pred_align_pharaoh = build_align_pharaoh(pred_align)
            pred_align_sent = ' '.join(pred_align_pharaoh)
            msg.append("ALIGN: {}\n".format(pred_align_sent))

        if self.gold_sent is not None:
            tgt_sent = ' '.join(self.gold_sent)
            msg.append('GOLD {}: {}\n'.format(sent_number, tgt_sent))
            msg.append(("GOLD SCORE: {:.4f}\n".format(self.gold_score)))
        if len(self.pred_sents) > 1:
            msg.append('\nBEST HYP:\n')
            for score, sent in zip(self.pred_scores, self.pred_sents):
                msg.append("[{:.4f}] {}\n".format(score, sent))

        return "".join(msg)
