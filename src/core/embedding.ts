import type { SearchEngine } from "./core.js";
import type { CatalogProvider } from "./bm25.js";
import type { CatalogSnapshot } from "./catalog.js";
import type { Embedder } from "./embedder.js";
import type {
  SearchFilters,
  SearchQueryInput,
  SearchQueryResult,
  ToolCard,
  ToolSearchHit
} from "../shared/index.js";

export interface EmbeddingSearchOptions {
  defaultTopK?: number;
  minScore?: number;
}

const DEFAULT_OPTIONS: Required<EmbeddingSearchOptions> = {
  defaultTopK: 20,
  minScore: 0
};

interface ToolEmbeddingEntry {
  tool: ToolCard;
  vector: Float32Array;
}

function normalizeFilterList(values?: string[]): Set<string> {
  return new Set((values ?? []).map((value) => value.toLowerCase()));
}

function matchesFilters(tool: ToolCard, filters?: SearchFilters): boolean {
  if (!filters) {
    return true;
  }

  if (filters.serverIds && filters.serverIds.length > 0) {
    const allowed = normalizeFilterList(filters.serverIds);
    if (!allowed.has(tool.serverId.toLowerCase())) {
      return false;
    }
  }

  if (filters.sideEffects && filters.sideEffects.length > 0) {
    const sideEffect = tool.sideEffect ?? "none";
    if (!filters.sideEffects.includes(sideEffect)) {
      return false;
    }
  }

  if (filters.tags && filters.tags.length > 0) {
    const wanted = normalizeFilterList(filters.tags);
    const hasTag = tool.tags.some((tag) => wanted.has(tag.toLowerCase()));
    if (!hasTag) {
      return false;
    }
  }

  return true;
}

function dotProduct(left: Float32Array, right: Float32Array): number {
  if (left.length !== right.length) {
    return 0;
  }

  let sum = 0;
  for (let i = 0; i < left.length; i += 1) {
    sum += (left[i] ?? 0) * (right[i] ?? 0);
  }
  return sum;
}

function magnitude(vector: Float32Array): number {
  let sumSquares = 0;
  for (let i = 0; i < vector.length; i += 1) {
    const value = vector[i] ?? 0;
    sumSquares += value * value;
  }
  return Math.sqrt(sumSquares);
}

function cosineSimilarity(left: Float32Array, right: Float32Array): number {
  const leftMag = magnitude(left);
  const rightMag = magnitude(right);
  if (leftMag === 0 || rightMag === 0) {
    return 0;
  }

  return dotProduct(left, right) / (leftMag * rightMag);
}

function buildEmbeddingText(tool: ToolCard, snapshot: CatalogSnapshot): string {
  const doc = snapshot.docs.get(tool.toolId);
  if (!doc) {
    return [
      tool.toolName,
      tool.title ?? "",
      tool.description ?? "",
      tool.tags.join(" "),
      tool.synonyms.join(" "),
      tool.args.map((arg) => arg.name).join(" "),
      tool.args.map((arg) => arg.description ?? "").join(" "),
      tool.examples.map((example) => `${example.query} ${example.callHint ?? ""}`).join(" "),
      tool.serverId
    ]
      .join("\n")
      .trim();
  }

  return [
    doc.name,
    doc.title,
    doc.description,
    doc.tags,
    doc.synonyms,
    doc.argNames,
    doc.argDescs,
    doc.examples,
    doc.serverId
  ]
    .join("\n")
    .trim();
}

export class EmbeddingSearchEngine implements SearchEngine {
  private options: Required<EmbeddingSearchOptions>;
  private catalogVersion = -1;
  private indexedVersion = -1;
  private snapshot: CatalogSnapshot | null = null;
  private isBuilding = false;
  private entries = new Map<string, ToolEmbeddingEntry>();

  constructor(private catalog: CatalogProvider, private embedder: Embedder, options: EmbeddingSearchOptions = {}) {
    this.options = {
      ...DEFAULT_OPTIONS,
      ...options
    };
  }

  async buildIndex(): Promise<void> {
    if (this.isBuilding) {
      return;
    }

    const snapshot = this.catalog.getSnapshot();
    if (snapshot.version === this.indexedVersion) {
      return;
    }

    this.isBuilding = true;
    try {
      const tools = Array.from(snapshot.tools.values());
      const texts = tools.map((tool) => buildEmbeddingText(tool, snapshot));
      const vectors = await this.embedder.embedBatch(texts);

      const nextEntries = new Map<string, ToolEmbeddingEntry>();
      for (let i = 0; i < tools.length; i += 1) {
        const tool = tools[i];
        const vector = vectors[i];
        if (!tool || !vector || vector.length === 0) {
          continue;
        }
        nextEntries.set(tool.toolId, { tool, vector });
      }

      this.entries = nextEntries;
      this.snapshot = snapshot;
      this.catalogVersion = snapshot.version;
      this.indexedVersion = snapshot.version;
    } finally {
      this.isBuilding = false;
    }
  }

  query(input: SearchQueryInput): SearchQueryResult {
    const snapshot = this.catalog.getSnapshot();
    if (snapshot.version !== this.catalogVersion) {
      this.snapshot = snapshot;
      this.catalogVersion = snapshot.version;
      void this.buildIndex().catch(() => undefined);
    }

    if (this.entries.size === 0 || this.indexedVersion !== snapshot.version) {
      return {
        hits: [],
        candidates: {
          before: snapshot.docs.size,
          after: 0
        }
      };
    }

    const queryVector = this.embedder.embedSync(input.query);
    if (!queryVector || queryVector.length === 0) {
      void this.embedder.embed(input.query).catch(() => undefined);
      return {
        hits: [],
        candidates: {
          before: snapshot.docs.size,
          after: 0
        }
      };
    }

    const topK = Math.max(0, input.topK ?? this.options.defaultTopK);
    const hits: ToolSearchHit[] = [];

    for (const { tool, vector } of this.entries.values()) {
      if (!matchesFilters(tool, input.filters)) {
        continue;
      }

      const score = cosineSimilarity(queryVector, vector);
      if (!Number.isFinite(score) || score <= this.options.minScore) {
        continue;
      }

      hits.push({
        toolId: tool.toolId,
        score
      });
    }

    hits.sort((a, b) => {
      if (b.score !== a.score) {
        return b.score - a.score;
      }
      return a.toolId.localeCompare(b.toolId);
    });

    return {
      hits: topK > 0 ? hits.slice(0, topK) : [],
      candidates: {
        before: snapshot.docs.size,
        after: hits.length
      }
    };
  }
}
