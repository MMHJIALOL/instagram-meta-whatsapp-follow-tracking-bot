// Intentionally empty by default.
// Add Drizzle tables here when the site actually needs a database.
// See examples/d1/db/schema.ts for an opt-in example.
import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
export const links = sqliteTable("join_links", {
  code: text("code").primaryKey(), target: text("target").notNull(),
  state: text("state").notNull(), updated: text("updated").notNull(),
});
export const events = sqliteTable("join_events", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  code: text("code").notNull(), at: text("at").notNull(),
  ua: text("ua").notNull(), ip: text("ip").notNull(),
});
