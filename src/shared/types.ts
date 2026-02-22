export type SideEffect = "none" | "read" | "write" | "destructive";
export type CostHint = "low" | "medium" | "high";
export type SearchMode = "bm25" | "regex";

export interface ToolArgCard {
  name: string;
  description?: string;
  typeHint?: string;
  required?: boolean;
  example?: string;
}

export interface ToolExample {
  query: string;
  callHint?: string;
}

export interface ToolCard {
  toolId: string; // "{serverId}:{toolName}"
  toolName: string;
  serverId: string;

  title?: string;
  description?: string;

  tags: string[];
  synonyms: string[];
  args: ToolArgCard[];
  examples: ToolExample[];

  sideEffect?: SideEffect;
  openWorldHint?: boolean;
  idempotentHint?: boolean;

  authHint: string[];
  costHint?: CostHint;

  popularity?: number;
  updatedAt?: string; // ISO8601
}

export interface ToolSearchDoc {
  id: string;
  name: string;
  title: string;
  description: string;
  tags: string;
  synonyms: string;
  argNames: string;
  argDescs: string;
  examples: string;
  serverId: string;
}

export interface Bm25FieldWeights {
  name: number;
  title: number;
  synonyms: number;
  description: number;
  argNames: number;
  argDescs: number;
  tags: number;
  examples: number;
  serverId: number;
}

export const DEFAULT_WEIGHTS: Bm25FieldWeights = {
  name: 4.0,
  title: 2.0,
  synonyms: 2.5,
  description: 1.8,
  argNames: 1.4,
  argDescs: 1.2,
  tags: 1.2,
  examples: 0.9,
  serverId: 0.2,
};

export interface ToolSearchHit {
  toolId: string;
  score: number;
  debug?: Record<string, unknown>;
}

export interface SearchFilters {
  serverIds?: string[];
  sideEffects?: SideEffect[];
  tags?: string[];
}

export interface SearchQueryInput {
  sessionId?: string;
  query: string;
  topK?: number;
  filters?: SearchFilters;
  weights?: Partial<Bm25FieldWeights>;
  mode?: SearchMode;
}

export interface SearchQueryResult {
  hits: ToolSearchHit[];
  candidates?: {
    before: number;
    after: number;
  };
}

export interface WorkingSetEntry {
  toolId: string;
  pinned: boolean;
  lastUsedAt: number;
  lastSelectedAt: number;
  ttlMs?: number;
  tokenCost: number;
  scoreHint?: number;
}

export interface WorkingSetState {
  sessionId: string;
  entries: Record<string, WorkingSetEntry>;
  budgetTokens: number;
  usedTokens: number;
}

export interface WorkingSetUpdateInput {
  sessionId: string;
  query: string;
  topK?: number;
  budgetTokens: number;
  pin?: string[];
  unpin?: string[];
  mode?: SearchMode;
}

export interface WorkingSetUpdateResult {
  selectedToolIds: string[];
  addedToolIds: string[];
  removedToolIds: string[];
  budgetUsed: number;
  budgetTotal?: number;
}

export interface ReducedToolResult {
  text: string;
  structured?: object;
  droppedBytes: number;
  droppedTokensEstimate: number;
  notes: string[];
}
