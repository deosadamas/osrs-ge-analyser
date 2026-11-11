package com.geanalyser;

import com.google.gson.Gson;
import com.google.inject.Provides;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import javax.inject.Inject;
import javax.swing.SwingUtilities;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.GrandExchangeOfferState;
import net.runelite.api.Item;
import net.runelite.api.ItemContainer;
import net.runelite.api.Player;
import net.runelite.api.WorldType;
import net.runelite.api.events.GameStateChanged;
import net.runelite.api.events.GameTick;
import net.runelite.api.events.GrandExchangeOfferChanged;
import net.runelite.api.events.ItemContainerChanged;
import net.runelite.api.gameval.InventoryID;
import net.runelite.api.gameval.ItemID;
import net.runelite.api.gameval.VarPlayerID;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.game.ItemManager;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.NavigationButton;
import net.runelite.client.util.ImageUtil;

/**
 * Shows the flips suggested by the osrs-ge-analyser CLI next to the game, records your
 * real GE offers for its `sync` command and keeps status.json up to date for its
 * `execute` loop. It only reads the game and writes files: it never places, changes or
 * cancels an offer.
 */
@Slf4j
@PluginDescriptor(
	name = "GE Analyser",
	description = "Flip suggestions from osrs-ge-analyser, a trade log for sync and a live status file for execute",
	tags = {"grand", "exchange", "flipping", "merch"}
)
public class GeAnalyserPlugin extends Plugin
{
	private static final Path DATA_DIR = Paths.get(System.getProperty("user.home"), ".osrs-ge-analyser");
	/** The client empties every GE slot while logging in or hopping and the server re-sends them over the next ticks. */
	private static final int LOGIN_SETTLE_TICKS = 5;
	/** If the inventory has still not arrived this many ticks later, it is empty. */
	private static final int INVENTORY_WAIT_TICKS = 20;
	/** status.json is rewritten at least this often while logged in, so its age shows it is being kept. */
	private static final long HEARTBEAT_SECONDS = 60;
	private static final int FREE_SLOTS = 3;
	private static final int MEMBER_SLOTS = 8;
	private static final long COINS_PER_PLATINUM_TOKEN = 1000;

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

	private StatusWriter statusWriter;
	private Path restoredFrom;
	private boolean loggingIn = true;
	private int settledAtTick = Integer.MAX_VALUE;
	private int looks; // times the offers were read since logging in
	private volatile boolean statusDirty; // also set by the writer thread when a write fails
	private volatile String lastStatus;
	private long lastStatusWrite;
	private final AtomicReference<String> statusToWrite = new AtomicReference<>();

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
		statusWriter = new StatusWriter(gson);
		restoredFrom = null;
		lastStatus = null;
		lastStatusWrite = 0;
		looks = 0;
		statusDirty = true;
		// Enabled while already logged in: the offers are settled, there is no login to wait for.
		loggingIn = client.getGameState() != GameState.LOGGED_IN;
		settledAtTick = loggingIn ? Integer.MAX_VALUE : client.getTickCount();
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
		statusWriter = null;
	}

	private Path adviceFile()
	{
		String custom = config.adviceFile();
		return custom == null || custom.trim().isEmpty() ? DATA_DIR.resolve("advice.json") : Paths.get(custom.trim());
	}

	/** Same default as the CLI: GE_ANALYSER_EXEC, else ~/.osrs-ge-analyser/exec. */
	private Path statusFile()
	{
		String custom = config.statusFile();
		if (custom != null && !custom.trim().isEmpty())
		{
			return Paths.get(custom.trim());
		}
		String dir = System.getenv("GE_ANALYSER_EXEC");
		return (dir == null || dir.trim().isEmpty() ? DATA_DIR.resolve("exec") : Paths.get(dir.trim())).resolve("status.json");
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
	public void onGameStateChanged(GameStateChanged event)
	{
		switch (event.getGameState())
		{
			case LOGGING_IN:
			case HOPPING:
			case CONNECTION_LOST:
			case LOGIN_SCREEN:
				loggingIn = true;
				settledAtTick = Integer.MAX_VALUE;
				break;
			case LOGGED_IN:
				if (loggingIn) // not the LOADING -> LOGGED_IN of walking into a new area
				{
					loggingIn = false;
					settledAtTick = client.getTickCount() + LOGIN_SETTLE_TICKS;
					looks = 0;
					statusDirty = true;
				}
				break;
			default:
				break;
		}
	}

	@Subscribe
	public void onGrandExchangeOfferChanged(GrandExchangeOfferChanged event)
	{
		statusDirty = true;
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

	@Subscribe
	public void onItemContainerChanged(ItemContainerChanged event)
	{
		if (event.getContainerId() == InventoryID.INV)
		{
			statusDirty = true;
		}
	}

	/** Describes offers, coins and inventory in status.json whenever one of them changed. Reads only. */
	@Subscribe
	public void onGameTick(GameTick tick)
	{
		if (statusWriter == null || !config.writeStatus() || client.getGameState() != GameState.LOGGED_IN || client.getTickCount() < settledAtTick)
		{
			return;
		}
		long now = Instant.now().getEpochSecond();
		if (!statusDirty && now - lastStatusWrite < HEARTBEAT_SECONDS)
		{
			return;
		}
		Player me = client.getLocalPlayer();
		GrandExchangeOffer[] offers = client.getGrandExchangeOffers();
		ItemContainer inventory = client.getItemContainer(InventoryID.INV);
		if (me == null || me.getName() == null || offers == null
			|| (inventory == null && client.getTickCount() < settledAtTick + INVENTORY_WAIT_TICKS))
		{
			return; // not all here yet: look again next tick
		}
		Path file = statusFile();
		if (!file.equals(restoredFrom))
		{
			restoredFrom = file;
			restore(file);
		}

		List<StatusWriter.Offer> seen = new ArrayList<>();
		for (int slot = 0; slot < offers.length; slot++)
		{
			GrandExchangeOffer o = offers[slot];
			if (o == null || o.getState() == GrandExchangeOfferState.EMPTY || o.getItemId() <= 0)
			{
				continue;
			}
			seen.add(new StatusWriter.Offer(slot, o.getState().name(), o.getItemId(), itemManager.getItemComposition(o.getItemId()).getName(),
				o.getPrice(), o.getTotalQuantity(), o.getQuantitySold(), o.getSpent()));
		}

		long cash = 0;
		Map<Integer, Long> stacks = new LinkedHashMap<>();
		if (inventory != null)
		{
			for (Item item : inventory.getItems())
			{
				if (item == null || item.getId() <= 0 || item.getQuantity() <= 0)
				{
					continue;
				}
				if (item.getId() == ItemID.COINS)
				{
					cash += item.getQuantity();
				}
				else if (item.getId() == ItemID.PLATINUM)
				{
					// Since 30 Sep 2026 the GE takes payment in platinum tokens as well as coins.
					cash += item.getQuantity() * COINS_PER_PLATINUM_TOKEN;
				}
				else
				{
					stacks.merge(itemManager.canonicalize(item.getId()), (long) item.getQuantity(), Long::sum);
				}
			}
		}
		List<StatusWriter.Stack> held = new ArrayList<>();
		for (Map.Entry<Integer, Long> e : stacks.entrySet())
		{
			held.add(new StatusWriter.Stack(e.getKey(), itemManager.getItemComposition(e.getKey()).getName(), e.getValue()));
		}

		// Items follow the world you are on; slots follow the account.
		boolean membersWorld = client.getWorldType().contains(WorldType.MEMBERS);
		boolean member = membersWorld || client.getVarpValue(VarPlayerID.ACCOUNT_CREDIT) > 0;
		String status = statusWriter.update(now, me.getName(), looks++ > 0, seen, cash, held, membersWorld, member ? MEMBER_SLOTS : FREE_SLOTS);
		statusDirty = false;
		if (status.equals(lastStatus) && now - lastStatusWrite < HEARTBEAT_SECONDS)
		{
			return;
		}
		lastStatus = status;
		lastStatusWrite = now;
		statusToWrite.set(status);
		executor.execute(() -> writeStatus(file));
	}

	private void restore(Path file)
	{
		try
		{
			if (Files.exists(file))
			{
				statusWriter.restore(new String(Files.readAllBytes(file), StandardCharsets.UTF_8));
			}
		}
		catch (IOException e)
		{
			log.warn("could not read {}", file, e);
		}
	}

	/** Off the client thread. Replaces the file in one step so the CLI never reads half of it. */
	private void writeStatus(Path file)
	{
		String status = statusToWrite.getAndSet(null);
		if (status == null)
		{
			return; // a later write already took the newest text
		}
		try
		{
			Files.createDirectories(file.getParent());
			Path tmp = file.resolveSibling(file.getFileName() + ".rl-tmp");
			Files.write(tmp, status.getBytes(StandardCharsets.UTF_8));
			try
			{
				Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
			}
			catch (AtomicMoveNotSupportedException e)
			{
				Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
			}
		}
		catch (IOException e)
		{
			log.warn("could not write {}", file, e);
			lastStatus = null; // try again on the next tick
			statusDirty = true;
		}
	}
}
