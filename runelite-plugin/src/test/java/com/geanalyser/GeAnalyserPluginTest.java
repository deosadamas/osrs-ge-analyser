package com.geanalyser;

import net.runelite.client.RuneLite;
import net.runelite.client.externalplugins.ExternalPluginManager;

/** Development launcher: `gradlew test --tests GeAnalyserPluginTest` or run from an IDE. */
public class GeAnalyserPluginTest
{
	public static void main(String[] args) throws Exception
	{
		ExternalPluginManager.loadBuiltin(GeAnalyserPlugin.class);
		RuneLite.main(args);
	}
}
