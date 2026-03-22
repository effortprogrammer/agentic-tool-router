import type { SearchEngine } from "./core.js";
import type { SearchQueryInput, SearchQueryResult } from "../shared/index.js";

export interface FusionStrategy {
  fuse(results: SearchQueryResult[], topK: number): SearchQueryResult;
}

export class RRFStrategy implements FusionStrategy {
  constructor(private readonly k: number = 60) {}

  fuse(results: SearchQueryResult[], topK: number): SearchQueryResult {
    const scores = new Map<string, number>();

    for (const result of results) {
      for (let rank = 0; rank < result.hits.length; rank += 1) {
        const hit = result.hits[rank];
        if (!hit) {
          continue;
        }
        const rrf = 1 / (this.k + rank + 1);
        scores.set(hit.toolId, (scores.get(hit.toolId) ?? 0) + rrf);
      }
    }

    const fusedHits = Array.from(scores.entries())
      .map(([toolId, score]) => ({ toolId, score }))
      .sort((a, b) => {
        if (b.score !== a.score) {
          return b.score - a.score;
        }
        return a.toolId.localeCompare(b.toolId);
      });

    const limitedHits = topK > 0 ? fusedHits.slice(0, topK) : [];
    const before = results.reduce((max, result) => Math.max(max, result.candidates?.before ?? 0), 0);

    return {
      hits: limitedHits,
      candidates: {
        before,
        after: fusedHits.length
      }
    };
  }
}

export interface HybridSearchOptions {
  defaultTopK?: number;
}

const DEFAULT_OPTIONS: Required<HybridSearchOptions> = {
  defaultTopK: 20
};

export class HybridSearchEngine implements SearchEngine {
  private options: Required<HybridSearchOptions>;

  constructor(
    private readonly engines: SearchEngine[],
    private readonly strategy: FusionStrategy = new RRFStrategy(),
    options: HybridSearchOptions = {}
  ) {
    this.options = {
      ...DEFAULT_OPTIONS,
      ...options
    };
  }

  query(input: SearchQueryInput): SearchQueryResult {
    const topK = Math.max(0, input.topK ?? this.options.defaultTopK);
    if (topK === 0 || this.engines.length === 0) {
      return {
        hits: [],
        candidates: {
          before: 0,
          after: 0
        }
      };
    }

    const perEngine = this.engines.map((engine) =>
      engine.query({
        ...input,
        topK
      })
    );

    return this.strategy.fuse(perEngine, topK);
  }
}
