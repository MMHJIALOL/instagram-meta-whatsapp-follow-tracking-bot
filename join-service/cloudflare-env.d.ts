declare namespace Cloudflare {
  interface Env {
    SYNC_TOKEN?: string;
    DB?: D1Database;
    BUCKET?: R2Bucket;
  }
}
