import re
import logging
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from fuzzywuzzy import fuzz

logger = logging.getLogger(__name__)

class SKUMatcher:
    def __init__(self, product_metrics_df, similarity_threshold=0.6, fuzzy_threshold=90):
        self.product_metrics_df = product_metrics_df
        self.similarity_threshold = similarity_threshold
        self.fuzzy_threshold = fuzzy_threshold
        self.tfidf_vectorizer = TfidfVectorizer(analyzer='word', ngram_range=(1, 2), min_df=1)
        self.tfidf_matrix = None
        self.cleaned_sku_names = {}
        self.product_codes = {}
        self.all_cleaned_skus = []
        self.sku_to_original = {}
        
        self._preprocess_skus()

    def _clean_text_for_matching(self, text):
        """Clean and standardize text for better matching."""
        print("cleaning")
        if not isinstance(text, str):
            return ""
        text = text.lower().strip()
        text = re.sub(r'\b[a-z]{2}-\d{3}-', '', text, flags=re.IGNORECASE)
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        text = re.sub(r'\b(ad|promo|campaign|osia|sale|offer|special|discount|limited|edition|new|best|seller|the|and|or|with|for|in|on|by|at|to|of|a|an)\b', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _extract_product_code(self, text):
        """Extract product codes using regex."""
        if not isinstance(text, str):
            return None
        match = re.search(r'\b([A-Z]{2})-([0-9]{3})\b', text)
        if match:
            return match.group()
        match = re.search(r'\b(sku|prod|item|code|id)?[-_]?\d+\b', text.lower())
        if match:
            return match.group()
        match = re.search(r'\b[A-Z0-9]+-[A-Z0-9]+(-[A-Z0-9]+)?\b', text)
        if match:
            return match.group()
        return None

    def _extract_product_name(self, text):
        """Extract product name from ad text."""
        print("name extraction")
        if not isinstance(text, str):
            return None
        parts = re.split(r'[_\s]', text)
        if parts:
            main_part = parts[0]
            if re.search(r'[A-Z]+-[0-9]+-[A-Z]+', main_part):
                return main_part
        return None

    def _preprocess_skus(self):
        """Preprocess SKU names for matching."""
        print("Processing SKUs")
        for idx, row in self.product_metrics_df.iterrows():
            sku_name = row['sku_name']
            if isinstance(sku_name, str):
                cleaned_text = self._clean_text_for_matching(sku_name)
                self.cleaned_sku_names[sku_name] = cleaned_text
                self.all_cleaned_skus.append(cleaned_text)
                self.sku_to_original[cleaned_text] = sku_name
                code = self._extract_product_code(sku_name)
                if code:
                    self.product_codes[code] = sku_name
        
        if self.all_cleaned_skus:
            self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(self.all_cleaned_skus)
            logger.info(f"TF-IDF vectorizer trained on {len(self.all_cleaned_skus)} SKUs")
        else:
            self.tfidf_matrix = None
            logger.warning("No SKUs available for TF-IDF training")

    def match_product(self, ad_name):
        """Match ad_name to a product SKU using multiple techniques."""
        print("Matching products")
        if not isinstance(ad_name, str) or not ad_name.strip():
            logger.debug(f"No Match: Ad='{ad_name}' - Empty or invalid input")
            return None

        main_product_id = re.split(r'[_\s]', ad_name)[0] if re.split(r'[_\s]', ad_name) else None
        
        # Step 1: Exact product code matching
        ad_code = self._extract_product_code(ad_name)
        if ad_code and ad_code in self.product_codes:
            matched_sku = self.product_codes[ad_code]
            if main_product_id and (main_product_id.lower() in matched_sku.lower() or 
                                   matched_sku.lower() in main_product_id.lower()):
                logger.info(f"Product Code Match: Ad='{ad_name}', SKU='{matched_sku}', Code='{ad_code}'")
                return matched_sku
            logger.debug(f"Product Code Match Rejected: Ad='{ad_name}', SKU='{matched_sku}', Code='{ad_code}' - Main ID mismatch")

        # Step 2: Product name matching
        product_name = self._extract_product_name(ad_name)
        if product_name:
            for sku_name in self.cleaned_sku_names.keys():
                sku_parts = re.split(r'[_\s-]', sku_name)
                sku_core = sku_parts[-1] if len(sku_parts) > 1 else sku_name
                prod_parts = re.split(r'[_\s-]', product_name)
                prod_core = prod_parts[-1] if len(prod_parts) > 1 else product_name
                if fuzz.ratio(sku_core.lower(), prod_core.lower()) >= 70:
                    logger.info(f"Product Name Match: Ad='{ad_name}', Extracted='{product_name}', SKU='{sku_name}'")
                    return sku_name

        # Step 3: TF-IDF matching
        cleaned_ad_name = self._clean_text_for_matching(ad_name)
        if not cleaned_ad_name or self.tfidf_matrix is None or not self.all_cleaned_skus:
            logger.debug(f"No Match: Ad='{ad_name}' - Insufficient data for TF-IDF matching")
            return None

        ad_vector = self.tfidf_vectorizer.transform([cleaned_ad_name])
        cosine_similarities = cosine_similarity(ad_vector, self.tfidf_matrix).flatten()
        best_idx = np.argmax(cosine_similarities)
        best_score = cosine_similarities[best_idx]
        best_cleaned_sku = self.all_cleaned_skus[best_idx]
        best_match = self.sku_to_original[best_cleaned_sku]

        ad_core = re.sub(r'^[a-z]{2}-\d{3}-', '', cleaned_ad_name, flags=re.IGNORECASE).strip()
        sku_core = re.sub(r'^[a-z]{2}-\d{3}-', '', self.cleaned_sku_names[best_match], flags=re.IGNORECASE).strip()
        core_similarity = fuzz.ratio(ad_core, sku_core)

        if best_score >= self.similarity_threshold and core_similarity >= 60:
            net_margin = self.product_metrics_df[self.product_metrics_df['sku_name'] == best_match]['net_margin'].iloc[0]
            logger.info(f"TF-IDF Match: Ad='{ad_name}', SKU='{best_match}', Net Margin={net_margin}, Score={best_score:.4f}, Core Similarity={core_similarity}")
            return best_match
        logger.debug(f"Low TF-IDF Score: Ad='{ad_name}', Best Match='{best_match}', Score={best_score:.4f}, Core Similarity={core_similarity}")

        # Step 4: Fuzzy matching
        best_match = None
        best_score = 0
        for sku_name, cleaned_sku in self.cleaned_sku_names.items():
            score = fuzz.token_set_ratio(cleaned_ad_name, cleaned_sku)
            if score > best_score:
                best_score = score
                best_match = sku_name

        if best_score >= self.fuzzy_threshold:
            ad_core = re.sub(r'^[a-z]{2}-\d{3}-', '', cleaned_ad_name, flags=re.IGNORECASE).strip()
            sku_core = re.sub(r'^[a-z]{2}-\d{3}-', '', self.cleaned_sku_names[best_match], flags=re.IGNORECASE).strip()
            core_similarity = fuzz.ratio(ad_core, sku_core)
            if core_similarity >= 60:
                net_margin = self.product_metrics_df[self.product_metrics_df['sku_name'] == best_match]['net_margin'].iloc[0]
                logger.info(f"Fuzzy Match: Ad='{ad_name}', SKU='{best_match}', Net Margin={net_margin}, Score={best_score}, Core Similarity={core_similarity}")
                return best_match
            logger.debug(f"Fuzzy Match Rejected: Ad='{ad_name}', Best Match='{best_match}', Score={best_score}, Core Similarity={core_similarity}")
        else:
            logger.debug(f"No Match: Ad='{ad_name}', Best Match='{best_match}', Fuzzy Score={best_score}")
        return None

def match_product_to_sku(df, product_metrics_df, ad_name_column):
    """Apply SKU matching to a DataFrame."""
    matcher = SKUMatcher(product_metrics_df)
    df['matched_sku'] = df[ad_name_column].apply(matcher.match_product)
    matched_df = df[df['matched_sku'].notna()][[ad_name_column, 'matched_sku']].drop_duplicates()
    logger.info("\nFinal Matched Products:")
    for _, row in matched_df.iterrows():
        logger.info(f"Ad Name: {row[ad_name_column]}, Matched SKU: {row['matched_sku']}")
    df = df.merge(product_metrics_df, left_on='matched_sku', right_on='sku_name', how='left')
    return df