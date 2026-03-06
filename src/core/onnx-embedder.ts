import fs from "node:fs";
import path from "node:path";
import type { Embedder } from "./embedder.js";

interface OnnxRuntimeTensor {
  data: Float32Array | BigInt64Array;
  dims: number[];
}

interface Ortx {
  InferenceSession: {
    create(modelPath: string): Promise<OnnxRuntimeSession>;
  };
  Tensor: new (type: string, data: Float32Array | BigInt64Array, dims: number[]) => unknown;
}

interface OnnxRuntimeSession {
  readonly inputNames: string[];
  readonly outputNames: string[];
  run(feeds: Record<string, unknown>): Promise<Record<string, OnnxRuntimeTensor>>;
  release?(): Promise<void> | void;
}

interface TokenizerConfig {
  model?: {
    vocab?: Record<string, number>;
    unk_token?: string;
  };
  vocab?: Record<string, number>;
}

export interface OnnxEmbedderOptions {
  modelPath: string;
  tokenizerPath: string;
  maxLength?: number;
  normalize?: boolean;
}

const DEFAULT_MAX_LENGTH = 256;

function toBigInt64(values: number[]): BigInt64Array {
  const output = new BigInt64Array(values.length);
  for (let i = 0; i < values.length; i += 1) {
    output[i] = BigInt(values[i] ?? 0);
  }
  return output;
}

function l2Normalize(vector: Float32Array): Float32Array {
  let normSquared = 0;
  for (let i = 0; i < vector.length; i += 1) {
    const value = vector[i] ?? 0;
    normSquared += value * value;
  }

  const norm = Math.sqrt(normSquared);
  if (norm === 0) {
    return vector;
  }

  const normalized = new Float32Array(vector.length);
  for (let i = 0; i < vector.length; i += 1) {
    normalized[i] = (vector[i] ?? 0) / norm;
  }
  return normalized;
}

function basicTokenize(text: string): string[] {
  const normalized = text.toLowerCase().trim();
  if (!normalized) {
    return [];
  }

  const matches = normalized.match(/[a-z0-9]+|[^\s]/g);
  return matches ?? [];
}

function isFloat32Data(data: Float32Array | BigInt64Array): data is Float32Array {
  return data instanceof Float32Array;
}

export class OnnxEmbedder implements Embedder {
  private session: OnnxRuntimeSession | null = null;
  private ort: Ortx | null = null;
  private readonly cache = new Map<string, Float32Array>();
  private readonly maxLength: number;
  private readonly normalize: boolean;
  private readonly vocab: Map<string, number>;
  private readonly unkToken: string;
  private dims = 0;
  private readonly modelPath: string;

  constructor(options: OnnxEmbedderOptions) {
    this.modelPath = path.resolve(options.modelPath);
    this.maxLength = Math.max(8, options.maxLength ?? DEFAULT_MAX_LENGTH);
    this.normalize = options.normalize ?? true;

    const tokenizerRaw = fs.readFileSync(path.resolve(options.tokenizerPath), "utf8");
    const tokenizerJson = JSON.parse(tokenizerRaw) as TokenizerConfig;
    const rawVocab = tokenizerJson.model?.vocab ?? tokenizerJson.vocab ?? {};
    this.vocab = new Map<string, number>(Object.entries(rawVocab));
    this.unkToken = tokenizerJson.model?.unk_token ?? "[UNK]";
  }

  dimensions(): number {
    return this.dims;
  }

  embedSync(text: string): Float32Array | null {
    return this.cache.get(text) ?? null;
  }

  async embed(text: string): Promise<Float32Array> {
    const cached = this.cache.get(text);
    if (cached) {
      return cached;
    }

    const [vector] = await this.embedBatch([text]);
    if (!vector) {
      return new Float32Array(0);
    }
    return vector;
  }

  async embedBatch(texts: string[]): Promise<Float32Array[]> {
    if (texts.length === 0) {
      return [];
    }

    const session = await this.getSession();
    const results: Float32Array[] = new Array(texts.length);

    const uncachedIndexes: number[] = [];
    const encodedInputs: number[][] = [];

    for (let i = 0; i < texts.length; i += 1) {
      const text = texts[i] ?? "";
      const cached = this.cache.get(text);
      if (cached) {
        results[i] = cached;
        continue;
      }

      uncachedIndexes.push(i);
      encodedInputs.push(this.encode(text));
    }

    if (uncachedIndexes.length === 0) {
      return results;
    }

    const seqLength = Math.min(
      this.maxLength,
      encodedInputs.reduce((max, ids) => Math.max(max, ids.length), 2)
    );

    const batchSize = uncachedIndexes.length;
    const inputIds = new Array<number>(batchSize * seqLength).fill(0);
    const attentionMask = new Array<number>(batchSize * seqLength).fill(0);
    const tokenTypeIds = new Array<number>(batchSize * seqLength).fill(0);

    for (let row = 0; row < batchSize; row += 1) {
      const tokenIds = encodedInputs[row] ?? [];
      const length = Math.min(tokenIds.length, seqLength);
      for (let col = 0; col < length; col += 1) {
        const offset = row * seqLength + col;
        inputIds[offset] = tokenIds[col] ?? 0;
        attentionMask[offset] = 1;
      }
    }

    const feeds: Record<string, unknown> = {};
    const ort = this.ort;
    if (!ort) {
      throw new Error("onnxruntime-node runtime is unavailable.");
    }
    for (const inputName of session.inputNames) {
      if (inputName === "input_ids") {
        feeds[inputName] = new ort.Tensor("int64", toBigInt64(inputIds), [batchSize, seqLength]);
      } else if (inputName === "attention_mask") {
        feeds[inputName] = new ort.Tensor("int64", toBigInt64(attentionMask), [batchSize, seqLength]);
      } else if (inputName === "token_type_ids") {
        feeds[inputName] = new ort.Tensor("int64", toBigInt64(tokenTypeIds), [batchSize, seqLength]);
      }
    }

    const outputMap = await session.run(feeds);
    const firstOutputName = session.outputNames[0];
    const output = firstOutputName ? outputMap[firstOutputName] : undefined;
    if (!output || !isFloat32Data(output.data)) {
      for (const index of uncachedIndexes) {
        results[index] = new Float32Array(0);
      }
      return results;
    }

    const pooled = this.meanPool(output.data, output.dims, attentionMask, batchSize, seqLength);
    for (let i = 0; i < uncachedIndexes.length; i += 1) {
      const idx = uncachedIndexes[i] ?? 0;
      const text = texts[idx] ?? "";
      const vector = pooled[i] ?? new Float32Array(0);
      this.cache.set(text, vector);
      results[idx] = vector;
    }

    return results;
  }

  close(): void {
    this.cache.clear();
    const session = this.session;
    this.session = null;
    this.ort = null;

    if (session?.release) {
      void session.release();
    }
  }

  private async getSession(): Promise<OnnxRuntimeSession> {
    if (this.session) {
      return this.session;
    }

    const dynamicImport = new Function("moduleName", "return import(moduleName);") as (
      moduleName: string
    ) => Promise<unknown>;
    const ortImport = (await dynamicImport("onnxruntime-node").catch(() => null)) as Ortx | null;
    if (!ortImport) {
      throw new Error("onnxruntime-node is not installed. Install it to use OnnxEmbedder.");
    }

    this.ort = ortImport;
    this.session = await ortImport.InferenceSession.create(this.modelPath);
    return this.session;
  }

  private encode(text: string): number[] {
    const cls = this.lookupToken("[CLS]");
    const sep = this.lookupToken("[SEP]");
    const pieces = basicTokenize(text);

    const ids: number[] = [cls];
    for (const piece of pieces) {
      for (const tokenId of this.wordpiece(piece)) {
        ids.push(tokenId);
        if (ids.length >= this.maxLength - 1) {
          break;
        }
      }
      if (ids.length >= this.maxLength - 1) {
        break;
      }
    }

    ids.push(sep);
    return ids;
  }

  private wordpiece(token: string): number[] {
    if (!token) {
      return [this.lookupToken(this.unkToken)];
    }

    if (this.vocab.has(token)) {
      return [this.lookupToken(token)];
    }

    const out: number[] = [];
    let start = 0;

    while (start < token.length) {
      let end = token.length;
      let matched: string | null = null;

      while (start < end) {
        const sub = token.slice(start, end);
        const candidate = start > 0 ? `##${sub}` : sub;
        if (this.vocab.has(candidate)) {
          matched = candidate;
          break;
        }
        end -= 1;
      }

      if (!matched) {
        return [this.lookupToken(this.unkToken)];
      }

      out.push(this.lookupToken(matched));
      start = end;
    }

    return out;
  }

  private lookupToken(token: string): number {
    return this.vocab.get(token) ?? this.vocab.get(this.unkToken) ?? 0;
  }

  private meanPool(
    hidden: Float32Array,
    dims: number[],
    attentionMask: number[],
    batchSize: number,
    seqLength: number
  ): Float32Array[] {
    const hiddenSize = dims[2] ?? 0;
    if (hiddenSize <= 0) {
      return new Array(batchSize).fill(null).map(() => new Float32Array(0));
    }

    if (this.dims === 0) {
      this.dims = hiddenSize;
    }

    const vectors: Float32Array[] = [];

    for (let row = 0; row < batchSize; row += 1) {
      const pooled = new Float32Array(hiddenSize);
      let tokenCount = 0;

      for (let col = 0; col < seqLength; col += 1) {
        const mask = attentionMask[row * seqLength + col] ?? 0;
        if (mask === 0) {
          continue;
        }

        tokenCount += 1;
        const base = (row * seqLength + col) * hiddenSize;
        for (let d = 0; d < hiddenSize; d += 1) {
          const current = pooled[d] ?? 0;
          pooled[d] = current + (hidden[base + d] ?? 0);
        }
      }

      if (tokenCount > 0) {
        for (let d = 0; d < hiddenSize; d += 1) {
          pooled[d] = (pooled[d] ?? 0) / tokenCount;
        }
      }

      vectors.push(this.normalize ? l2Normalize(pooled) : pooled);
    }

    return vectors;
  }
}
