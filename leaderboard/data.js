window.LEADERBOARD_DATA = {
 "generated_at": "2026-08-27T14:41:21.773255+00:00",
 "sources": {
  "id": {
   "label": "In-distribution (synthetic, template-matched) \u2014 regression harness, NOT a capability claim",
   "rows": [
    {
     "bucket": "128k",
     "n_questions": 37,
     "mean": 1.0,
     "sd": 0.0,
     "seeds": {
      "42": 1.0,
      "44": 1.0,
      "45": 1.0,
      "46": 1.0,
      "47": 1.0
     },
     "baselines": {
      "vector_only": 0.6902,
      "bm25": 0.7016
     }
    },
    {
     "bucket": "500k",
     "n_questions": 72,
     "mean": 1.0,
     "sd": 0.0,
     "seeds": {
      "42": 1.0,
      "44": 1.0,
      "45": 1.0,
      "46": 1.0,
      "47": 1.0
     },
     "baselines": {
      "vector_only": 0.6792,
      "bm25": 0.7055
     }
    },
    {
     "bucket": "1m",
     "n_questions": 107,
     "mean": 1.0,
     "sd": 0.0,
     "seeds": {
      "42": 1.0,
      "44": 1.0,
      "45": 1.0,
      "46": 1.0,
      "47": 1.0
     },
     "baselines": {
      "vector_only": 0.7013,
      "bm25": 0.6878
     }
    },
    {
     "bucket": "10m",
     "n_questions": 216,
     "mean": 1.0,
     "sd": 0.0,
     "seeds": {
      "42": 1.0,
      "44": 1.0,
      "45": 1.0,
      "46": 1.0,
      "47": 1.0
     },
     "baselines": {
      "vector_only": 0.6615,
      "bm25": 0.6164
     }
    }
   ],
   "disclaimer": "The corpus generator and the pattern extractor were authored together; this measures template coverage, ceiling by construction."
  },
  "ood": {
   "label": "OOD paraphrase benchmark \u2014 independent LLM re-rendering of held-out phrasings (the honest generalization gap)",
   "rows": [
    {
     "style": "paraphrase",
     "extraction_recall": 0.0937,
     "extraction_sd": 0.0943,
     "renderer_omissions": 0.25,
     "e2e": 0.2821,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.2188,
      "EO": 0.1667,
      "IE": 0.3333,
      "IF": 0.25,
      "KU": 0.1667,
      "MH": 0.0,
      "PF": 0.0,
      "SZ": 0.75,
      "TR": 0.0833
     },
     "n_questions": 140,
     "enriched": null
    },
    {
     "style": "negation",
     "extraction_recall": 0.7562,
     "extraction_sd": 0.0329,
     "renderer_omissions": 0,
     "e2e": 0.6929,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.4375,
      "EO": 0.875,
      "IE": 0.6667,
      "IF": 0.375,
      "KU": 0.3333,
      "MH": 0.75,
      "PF": 0.5625,
      "SZ": 1.0,
      "TR": 0.9167
     },
     "n_questions": 140,
     "enriched": 0.6571
    },
    {
     "style": "indirect",
     "extraction_recall": 0.4488,
     "extraction_sd": 0.1025,
     "renderer_omissions": 0.25,
     "e2e": 0.4857,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.375,
      "EO": 0.75,
      "IE": 0.4167,
      "IF": 0.125,
      "KU": 0.3333,
      "MH": 0.0,
      "PF": 0.25,
      "SZ": 0.5,
      "TR": 0.5833
     },
     "n_questions": 140,
     "enriched": 0.4929
    },
    {
     "style": "informal",
     "extraction_recall": 0.0509,
     "extraction_sd": 0.0587,
     "renderer_omissions": 0,
     "e2e": 0.15,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.0,
      "EO": 0.0,
      "IE": 0.0,
      "IF": 0.5,
      "KU": 0.0,
      "MH": 0.125,
      "PF": 0.0,
      "SZ": 0.0,
      "TR": 0.0
     },
     "n_questions": 140,
     "enriched": 0.1714
    },
    {
     "style": "non_english",
     "extraction_recall": 0.0,
     "extraction_sd": 0.0,
     "renderer_omissions": 0.25,
     "e2e": 0.1571,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.0,
      "EO": 0.0,
      "IE": 0.125,
      "IF": 0.0,
      "KU": 0.0,
      "MH": 0.0,
      "PF": 0.0,
      "SZ": 0.5,
      "TR": 0.0833
     },
     "n_questions": 140,
     "enriched": 0.1643
    },
    {
     "style": "code_switch",
     "extraction_recall": 0.5788,
     "extraction_sd": 0.1806,
     "renderer_omissions": 0,
     "e2e": 0.6071,
     "per_ability": {
      "AB": 1.0,
      "CR": 0.375,
      "EO": 0.75,
      "IE": 0.5833,
      "IF": 0.0,
      "KU": 0.5,
      "MH": 0.625,
      "PF": 0.375,
      "SZ": 1.0,
      "TR": 0.8333
     },
     "n_questions": 140,
     "enriched": 0.5857
    }
   ],
   "n_personas": 4,
   "disclaimer": "Renderings produced by an LLM (glm-4-plus; identity recorded in results JSON); judged by the deterministic nugget judge; renderer omissions excluded from extraction recall."
  },
  "llm_judge_crosscheck": {
   "n_items": 58,
   "n_items_attempted": 240,
   "sampling_note": "API quota limited the run to 58 of 240 exported items; agreement stats are computed on this sample and labelled as such.",
   "det_judge_mean": 0.3448,
   "llm_judge_mean": 0.25,
   "exact_agreement": 0.7586,
   "within_half_point": 0.7759,
   "llm_judge_per_ability": {
    "AB": 1.0,
    "CR": 0.0,
    "EO": 0.5,
    "IE": 0.0,
    "IF": 0.0,
    "KU": 0.25,
    "MH": 0.0,
    "PF": 0.0,
    "SZ": 0.125,
    "TR": 0.25
   },
   "judge_models": [
    "glm-4-plus"
   ],
   "finding": "The LLM judge grades OOD contexts LOWER (0.250) than the deterministic judge (0.345): the deterministic grader is not inflating OOD scores relative to an independent LLM grader \u2014 if anything it is slightly generous.",
   "protocol": "BEAM-style context-sufficiency rubric replicated with the recorded judge model(s); canonical BEAM uses gpt-5 \u2014 numbers are NOT directly comparable across judge models."
  },
  "micro": {
   "latency_recall": {
    "n=10000": {
     "flat_ms": 22.87,
     "tree_p50_ms": 0.957,
     "tree_p99_ms": 2.156,
     "any_hit@10": 0.5,
     "overlap@10": 0.491,
     "quality_ratio": 0.8526,
     "index_build_s": 0.73,
     "rows_scanned_avg": 777.0
    },
    "n=50000": {
     "flat_ms": 109.32,
     "tree_p50_ms": 1.519,
     "tree_p99_ms": 3.115,
     "any_hit@10": 0.65,
     "overlap@10": 0.234,
     "quality_ratio": 0.8065,
     "index_build_s": 3.06,
     "rows_scanned_avg": 1287.0
    },
    "n=100000": {
     "flat_ms": 213.45,
     "tree_p50_ms": 1.248,
     "tree_p99_ms": 2.669,
     "any_hit@10": 0.46,
     "overlap@10": 0.098,
     "quality_ratio": 0.7753,
     "index_build_s": 4.46,
     "rows_scanned_avg": 1116.0
    }
   },
   "codec_ablation": {
    "int8": {
     "bytes_per_vector": 770,
     "mb_per_million": 770,
     "self_hit@10": 1.0,
     "overlap@10_vs_fp32": 0.9025,
     "recall@10_in_top50": 1.0,
     "encode_ms_per_1k": 11.08,
     "query_ms_flat_20k": 38.772
    },
    "binary": {
     "bytes_per_vector": 96,
     "mb_per_million": 96,
     "self_hit@10": 1.0,
     "overlap@10_vs_fp32": 0.419,
     "recall@10_in_top50": 1.0,
     "encode_ms_per_1k": 29.87,
     "query_ms_flat_20k": 2.724
    },
    "rabitq": {
     "bytes_per_vector": 96,
     "mb_per_million": 96,
     "self_hit@10": 1.0,
     "overlap@10_vs_fp32": 0.433,
     "recall@10_in_top50": 1.0,
     "encode_ms_per_1k": 34.26,
     "query_ms_flat_20k": 2.845
    },
    "pq": {
     "bytes_per_vector": 8,
     "mb_per_million": 8,
     "self_hit@10": 0.865,
     "overlap@10_vs_fp32": 0.2675,
     "recall@10_in_top50": 0.9995,
     "encode_ms_per_1k": 482.87,
     "query_ms_flat_20k": 1.091
    },
    "fp32_reference": {
     "bytes_per_vector": 3072,
     "mb_per_million": 3072.0,
     "recall@10": 1.0
    }
   },
   "self_healing": {
    "plain@0%": {
     "self_identification": 1.0
    },
    "plain@1%": {
     "self_identification": 1.0
    },
    "plain@5%": {
     "self_identification": 1.0
    },
    "plain@10%": {
     "self_identification": 1.0
    },
    "plain@20%": {
     "self_identification": 0.96
    },
    "tmr@0%": {
     "self_identification": 1.0
    },
    "tmr@1%": {
     "self_identification": 1.0
    },
    "tmr@5%": {
     "self_identification": 1.0
    },
    "tmr@10%": {
     "self_identification": 1.0
    },
    "tmr@20%": {
     "self_identification": 1.0
    }
   },
   "slb_replay": {
    "hit_rate": 0.705,
    "avg_hit_latency_us": 6.7,
    "avg_miss_latency_us": 6.3
   },
   "u0_llm_calls": 0
  }
 }
};
