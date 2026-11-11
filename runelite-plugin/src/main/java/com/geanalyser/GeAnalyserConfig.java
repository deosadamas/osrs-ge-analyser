package com.geanalyser;

import net.runelite.client.config.Config;
import net.runelite.client.config.ConfigGroup;
import net.runelite.client.config.ConfigItem;
import net.runelite.client.config.Range;

@ConfigGroup("geanalyser")
public interface GeAnalyserConfig extends Config
{
	@ConfigItem(
		keyName = "adviceFile",
		name = "Advice file",
		description = "JSON written by `ge-analyser advise`. Empty = ~/.osrs-ge-analyser/advice.json",
		position = 0
	)
	default String adviceFile()
	{
		return "";
	}

	@ConfigItem(
		keyName = "refreshSeconds",
		name = "Refresh (s)",
		description = "How often the panel re-reads the advice file",
		position = 1
	)
	@Range(min = 5, max = 600)
	default int refreshSeconds()
	{
		return 15;
	}

	@ConfigItem(
		keyName = "recordTrades",
		name = "Record my GE offers",
		description = "Append every offer change to ~/.osrs-ge-analyser/runelite-trades.jsonl for `ge-analyser sync`",
		position = 2
	)
	default boolean recordTrades()
	{
		return true;
	}

	@ConfigItem(
		keyName = "maxRows",
		name = "Rows",
		description = "How many suggestions to show",
		position = 3
	)
	@Range(min = 1, max = 50)
	default int maxRows()
	{
		return 15;
	}

	@ConfigItem(
		keyName = "writeStatus",
		name = "Write status.json",
		description = "Keep status.json for `ge-analyser execute` up to date with your GE offers, coins and inventory. Read-only: nothing in the game is touched",
		position = 4
	)
	default boolean writeStatus()
	{
		return true;
	}

	@ConfigItem(
		keyName = "statusFile",
		name = "Status file",
		description = "Where to write it. Empty = ~/.osrs-ge-analyser/exec/status.json (or the folder in GE_ANALYSER_EXEC). The instructions shown in the panel are the orders.json in the same folder",
		position = 5
	)
	default String statusFile()
	{
		return "";
	}
}
