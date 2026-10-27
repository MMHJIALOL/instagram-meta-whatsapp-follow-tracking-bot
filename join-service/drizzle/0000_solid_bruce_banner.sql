CREATE TABLE `join_events` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`code` text NOT NULL,
	`at` text NOT NULL,
	`ua` text NOT NULL,
	`ip` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `join_links` (
	`code` text PRIMARY KEY NOT NULL,
	`target` text NOT NULL,
	`state` text NOT NULL,
	`updated` text NOT NULL
);
