import type { RouterCore, SearchEngine } from "./core.js";
import { InMemoryCatalog } from "./catalog.js";
import { Bm25SearchEngine } from "./bm25.js";
import type { Bm25SearchOptions } from "./bm25.js";
import type { Embedder } from "./embedder.js";
import { EmbeddingSearchEngine } from "./embedding.js";
import { HybridSearchEngine, RRFStrategy } from "./hybrid.js";
import { RegexSearchEngine } from "./regex.js";
import { SimpleTokenizer } from "./tokenizer.js";
import type { TokenizerOptions } from "./tokenizer.js";
import { InMemoryWorkingSetManager } from "./working-set.js";
import type { WorkingSetOptions } from "./working-set.js";
import { DefaultResultReducer } from "./result-reducer.js";
import type { ResultReducerOptions } from "./result-reducer.js";
import type {
  SearchQueryInput,
  SearchQueryResult,
} from "../shared/index.js";

export interface RouterCoreOptions {
  tokenizer?: TokenizerOptions;
  bm25?: Bm25SearchOptions;
  hybrid?: boolean;
  embedder?: Embedder;
  workingSet?: WorkingSetOptions;
  resultReducer?: ResultReducerOptions;
}

class DualSearchEngine implements SearchEngine {
  constructor(
    private primary: SearchEngine,
    private regex: RegexSearchEngine,
  ) {}

  query(input: SearchQueryInput): SearchQueryResult {
    if (input.mode === "regex") {
      return this.regex.query(input);
    }
    return this.primary.query(input);
  }
}

export function createRouterCore(options: RouterCoreOptions = {}): RouterCore {
  const catalog = new InMemoryCatalog();
  const tokenizer = new SimpleTokenizer(options.tokenizer);
  const bm25 = new Bm25SearchEngine(catalog, tokenizer, options.bm25);
  const regex = new RegexSearchEngine(catalog);

  let primarySearch: SearchEngine = bm25;
  if (options.hybrid && options.embedder) {
    const embedding = new EmbeddingSearchEngine(catalog, options.embedder);
    void embedding.buildIndex().catch(() => undefined);
    primarySearch = new HybridSearchEngine([bm25, embedding], new RRFStrategy());
  }

  const search = new DualSearchEngine(primarySearch, regex);
  const workingSet = new InMemoryWorkingSetManager(
    catalog,
    search,
    options.workingSet,
  );
  const result = new DefaultResultReducer(options.resultReducer);

  return {
    catalog,
    search,
    workingSet,
    result,
  };
}
