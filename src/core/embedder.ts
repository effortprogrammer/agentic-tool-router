export interface Embedder {
  embed(text: string): Promise<Float32Array>;
  embedBatch(texts: string[]): Promise<Float32Array[]>;
  embedSync(text: string): Float32Array | null;
  dimensions(): number;
  close(): void;
}
