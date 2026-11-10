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
}
