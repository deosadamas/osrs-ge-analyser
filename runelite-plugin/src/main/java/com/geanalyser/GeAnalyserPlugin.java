package com.geanalyser;

import com.google.gson.Gson;
import com.google.inject.Provides;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import javax.inject.Inject;
import javax.swing.SwingUtilities;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.Player;
import net.runelite.api.events.GrandExchangeOfferChanged;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.game.ItemManager;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.NavigationButton;
import net.runelite.client.util.ImageUtil;

/**
 * Shows the flips suggested by the osrs-ge-analyser CLI next to the game and records
 * your real GE offers for its `sync` command. It never places or modifies offers.
 */
@Slf4j
@PluginDescriptor(
	name = "GE Analyser",
	description = "Flip suggestions from osrs-ge-analyser and a trade log for its sync command",
	tags = {"grand", "exchange", "flipping", "merch"}
)
public class GeAnalyserPlugin extends Plugin
{
	private static final Path DATA_DIR = Paths.get(System.getProperty("user.home"), ".osrs-ge-analyser");

	@Inject
	private Client client;

	@Inject
	private ClientToolbar clientToolbar;

	@Inject
	private ItemManager itemManager;

	@Inject
	private ScheduledExecutorService executor;

	@Inject
	private GeAnalyserConfig config;

	@Inject
	private Gson gson;

	private GeAnalyserPanel panel;
	private NavigationButton navButton;
	private ScheduledFuture<?> refresh;
	private TradeRecorder recorder;
	private long lastAdviceMtime = -1;

	@Provides
	GeAnalyserConfig provideConfig(ConfigManager configManager)
	{
		return configManager.getConfig(GeAnalyserConfig.class);
	}

	@Override
	protected void startUp()
	{
		panel = new GeAnalyserPanel();
		final BufferedImage icon = ImageUtil.loadImageResource(getClass(), "icon.png");
		navButton = NavigationButton.builder()
			.tooltip("GE Analyser")
			.icon(icon)
			.priority(6)
			.panel(panel)
			.build();
		clientToolbar.addNavigation(navButton);
		recorder = new TradeRecorder(DATA_DIR.resolve("runelite-trades.jsonl"), gson);
		refresh = executor.scheduleWithFixedDelay(this::reloadAdvice, 0, config.refreshSeconds(), TimeUnit.SECONDS);
	}

	@Override
	protected void shutDown()
	{
		if (refresh != null)
		{
			refresh.cancel(false);
		}
		clientToolbar.removeNavigation(navButton);
		panel = null;
		recorder = null;
	}

	private Path adviceFile()
	{
		String custom = config.adviceFile();
		return custom == null || custom.trim().isEmpty() ? DATA_DIR.resolve("advice.json") : Paths.get(custom.trim());
	}

	private void reloadAdvice()
	{
		Path file = adviceFile();
		try
		{
			if (!Files.exists(file))
			{
				if (lastAdviceMtime != -2)
				{
					lastAdviceMtime = -2;
					SwingUtilities.invokeLater(() -> panel.showMessage("No advice file at " + file + ".  Run `ge-analyser advise`."));
				}
				return;
			}
			long mtime = Files.getLastModifiedTime(file).toMillis();
			if (mtime == lastAdviceMtime)
			{
				return;
			}
			lastAdviceMtime = mtime;
			String json = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
			Advice advice = gson.fromJson(json, Advice.class);
			int max = config.maxRows();
			SwingUtilities.invokeLater(() -> panel.show(advice, max));
		}
		catch (IOException | RuntimeException e)
		{
			log.warn("could not read {}", file, e);
			SwingUtilities.invokeLater(() -> panel.showMessage("Could not read " + file + ": " + e.getMessage()));
		}
	}

	@Subscribe
	public void onGrandExchangeOfferChanged(GrandExchangeOfferChanged event)
	{
		if (!config.recordTrades() || recorder == null || client.getGameState() != GameState.LOGGED_IN)
		{
			return;
		}
		GrandExchangeOffer offer = event.getOffer();
		Player me = client.getLocalPlayer();
		String account = me != null && me.getName() != null ? me.getName() : "unknown";
		String name = offer.getItemId() > 0 ? itemManager.getItemComposition(offer.getItemId()).getName() : "";
		recorder.record(account, event.getSlot(), offer, name);
	}
}
