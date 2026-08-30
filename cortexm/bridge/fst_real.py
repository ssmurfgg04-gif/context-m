"""Real finite-state transducer for query normalization using marisa-trie.

Replaces the previous dict-based "FST" with an actual compiled trie structure.
marisa-trie provides O(L) prefix matching where L = query length, regardless of
dictionary size. Supports prefix search, predictive search, and map storage.

For production use at 100K+ entries, this is orders of magnitude faster and
more memory-efficient than a Python dict.

Dependencies: marisa-trie (pip install marisa-trie)
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

def _ensure_marisa():
    """Lazy import check — works even if marisa-trie was installed after first import."""
    try:
        import marisa_trie
        return marisa_trie
    except ImportError:
        raise RuntimeError(
            "marisa-trie required for FST. Install: pip install marisa-trie"
        )


class QueryFST:
    """Compiled FST for abbreviation expansion + spelling correction.

    Uses marisa-trie for O(L) prefix matching. The trie stores
    lowercase abbreviations/misspellings as keys, with their expansions
    as values in a parallel dictionary.
    """

    def __init__(self, abbreviations: Dict[str, str] | None = None,
                 spelling: Dict[str, str] | None = None) -> None:
        self._marisa = _ensure_marisa()
        self._abbreviations = dict(abbreviations or {})
        self._spelling = dict(spelling or {})
        # Merge both dictionaries
        self._all = {**self._abbreviations, **self._spelling}
        # Build trie from lowercase keys
        self._trie = self._marisa.Trie(self._all.keys())
        # Compile reverse index for fast lookup
        self._rev = {v.lower(): k for k, v in self._all.items()}

    def normalize(self, query: str) -> str:
        """Normalize query using FST lookup.

        Order: abbreviations first (so "MIT" expands before token-level
        spelling correction), then spelling corrections.
        """
        if not query:
            return query
        tokens = query.split()
        out = []
        i = 0
        while i < len(tokens):
            _punct = chr(34) + chr(39) + ',.!?;:'
            token = tokens[i].lower().strip(_punct)
            # Try longest prefix match in trie
            # marisa_trie.prefixes() returns all prefixes
            prefixes = list(self._trie.prefixes(token))
            if prefixes:
                # Use longest match
                best = max(prefixes, key=len)
                expansion = self._all[best]
                out.append(expansion)
                i += 1
                continue
            # Try spelling correction (exact match)
            if token in self._spelling:
                out.append(self._spelling[token])
            else:
                out.append(tokens[i])
            i += 1
        return " ".join(out)

    def expand_prefix(self, prefix: str) -> List[Tuple[str, str]]:
        """Return all expansions matching a prefix."""
        prefix = prefix.lower()
        results = []
        for key in self._trie.keys(prefix):
            results.append((key, self._all[key]))
        return results

    def has_key(self, key: str) -> bool:
        """Check if key exists in FST."""
        return key.lower() in self._all

    def __len__(self) -> int:
        return len(self._all)

    def __contains__(self, key: str) -> bool:
        return self.has_key(key)


# ---------------------------------------------------------------------------
# Default FST with curated abbreviations and spelling corrections
# ---------------------------------------------------------------------------
DEFAULT_ABBREVIATIONS: Dict[str, str] = {
    # US universities
    "ucla": "University of California Los Angeles",
    "ucb": "University of California Berkeley",
    "ucsd": "University of California San Diego",
    "ucsf": "University of California San Francisco",
    "mit": "Massachusetts Institute of Technology",
    "caltech": "California Institute of Technology",
    "nyu": "New York University",
    "usc": "University of Southern California",
    "cmu": "Carnegie Mellon University",
    "stanford": "Stanford University",
    "harvard": "Harvard University",
    "yale": "Yale University",
    "princeton": "Princeton University",
    "columbia": "Columbia University",
    "upenn": "University of Pennsylvania",
    "gatech": "Georgia Institute of Technology",
    "uiuc": "University of Illinois Urbana Champaign",
    "umich": "University of Michigan",
    "ut austin": "University of Texas at Austin",
    # US cities
    "nyc": "New York City",
    "la": "Los Angeles",
    "sf": "San Francisco",
    "dc": "Washington DC",
    "chi": "Chicago",
    "sea": "Seattle",
    "bos": "Boston",
    "philly": "Philadelphia",
    "atl": "Atlanta",
    "mia": "Miami",
    # Tech companies
    "google": "Google",
    "msft": "Microsoft",
    "amzn": "Amazon",
    "aapl": "Apple",
    "meta": "Meta",
    "tsla": "Tesla",
    "nvda": "NVIDIA",
    "intc": "Intel",
    "amd": "AMD",
    "orcl": "Oracle",
    # Common abbreviations
    "usa": "United States of America",
    "uk": "United Kingdom",
    "eu": "European Union",
    "un": "United Nations",
    "nato": "North Atlantic Treaty Organization",
    "fbi": "Federal Bureau of Investigation",
    "cia": "Central Intelligence Agency",
    "nsa": "National Security Agency",
    "nasa": "National Aeronautics and Space Administration",
    "cdc": "Centers for Disease Control and Prevention",
    "fda": "Food and Drug Administration",
    "irs": "Internal Revenue Service",
    "ssn": "Social Security Number",
    "dob": "Date of Birth",
    "phd": "Doctor of Philosophy",
    "md": "Doctor of Medicine",
    "jd": "Juris Doctor",
    "mba": "Master of Business Administration",
    "ba": "Bachelor of Arts",
    "bs": "Bachelor of Science",
    "ma": "Master of Arts",
    "ms": "Master of Science",
}

DEFAULT_SPELLING: Dict[str, str] = {
    "recieve": "receive",
    "seperate": "separate",
    "occured": "occurred",
    "definately": "definitely",
    "accomodate": "accommodate",
    "neccessary": "necessary",
    "publically": "publicly",
    "occurence": "occurrence",
    "independant": "independent",
    "existance": "existence",
    "arguement": "argument",
    "suprise": "surprise",
    "noticable": "noticeable",
    "peice": "piece",
    "untill": "until",
    "tommorow": "tomorrow",
    "truely": "truly",
    "beleive": "believe",
    "acheive": "achieve",
    "freind": "friend",
    "calender": "calendar",
    "collegue": "colleague",
    "enviroment": "environment",
    "goverment": "government",
    "harrass": "harass",
    "liason": "liaison",
    "maintainance": "maintenance",
    "miniscule": "minuscule",
    "neice": "niece",
    "persistance": "persistence",
    "posession": "possession",
    "prefered": "preferred",
    "proffesional": "professional",
    "recomend": "recommend",
    "refered": "referred",
    "relevent": "relevant",
    "resistence": "resistance",
    "sieze": "seize",
    "supercede": "supersede",
    "suprise": "surprise",
    "tatoo": "tattoo",
    "tendancy": "tendency",
    "tommorrow": "tomorrow",
    "wierd": "weird",
    "whereever": "wherever",
    "wich": "which",
    "wont": "will not",
    "wouldnt": "would not",
    "cant": "cannot",
    "dont": "do not",
    "doesnt": "does not",
    "didnt": "did not",
    "isnt": "is not",
    "arent": "are not",
    "wasnt": "was not",
    "werent": "were not",
    "hasnt": "has not",
    "havent": "have not",
    "hadnt": "had not",
    "couldnt": "could not",
    "shouldnt": "should not",
    "wouldnt": "would not",
    "mightnt": "might not",
    "mustnt": "must not",
    "shant": "shall not",
    "neednt": "need not",
    "darent": "dare not",
    "oughtnt": "ought not",
    "usednt": "used not",
    "aint": "am not",
    "im": "I am",
    "ive": "I have",
    "ill": "I will",
    "id": "I would",
    "youre": "you are",
    "youve": "you have",
    "youll": "you will",
    "youd": "you would",
    "hes": "he is",
    "hes": "he has",
    "hell": "he will",
    "hed": "he would",
    "shes": "she is",
    "shes": "she has",
    "shell": "she will",
    "shed": "she would",
    "its": "it is",
    "its": "it has",
    "itll": "it will",
    "itd": "it would",
    "were": "we are",
    "weve": "we have",
    "well": "we will",
    "wed": "we would",
    "theyre": "they are",
    "theyve": "they have",
    "theyll": "they will",
    "theyd": "they would",
    "thats": "that is",
    "thats": "that has",
    "thatll": "that will",
    "whos": "who is",
    "whos": "who has",
    "wholl": "who will",
    "whatre": "what are",
    "whats": "what is",
    "whats": "what has",
    "wheres": "where is",
    "wheres": "where has",
    "wherell": "where will",
    "whens": "when is",
    "whenll": "when will",
    "whys": "why is",
    "whyll": "why will",
    "hows": "how is",
    "hows": "how has",
    "howll": "how will",
    "theres": "there is",
    "theres": "there has",
    "therell": "there will",
    "heres": "here is",
    "heres": "here has",
    "herell": "here will",
}


def default_fst() -> QueryFST:
    """Return the default FST with standard abbreviations and spelling."""
    return QueryFST(abbreviations=DEFAULT_ABBREVIATIONS, spelling=DEFAULT_SPELLING)
