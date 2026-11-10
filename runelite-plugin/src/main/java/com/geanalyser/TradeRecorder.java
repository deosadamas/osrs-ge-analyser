package com.geanalyser;

import com.google.gson.Gson;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.GrandExchangeOfferState;

/**
 * Appends one JSON line per offer change to runelite-trades.jsonl. Field names match
 * ge_analyser/runelite.py::parse_plugin_log.
 */
@Slf4j
class TradeRecorder
{
	private final Path file;
	private final Gson gson;
	private final Map<Integer, Long> placedAt = new HashMap<>();
	private final Map<Integer, String> lastKey = new HashMap<>();

	TradeRecorder(Path file, Gson gson)
	{
		this.file = file;
		this.gson = gson;
	}

	void record(String account, int slot, GrandExchangeOffer offer, String itemName)
	{
		GrandExchangeOfferState state = offer.getState();
		long now = Instant.now().getEpochSecond();
		if (state == GrandExchangeOfferState.EMPTY || offer.getItemId() == 0)
		{
			placedAt.remove(slot);
			lastKey.remove(slot);
			return;
		}
		boolean buy = state == GrandExchangeOfferState.BUYING
			|| state == GrandExchangeOfferState.BOUGHT
			|| state == GrandExchangeOfferState.CANCELLED_BUY;
		int sold = offer.getQuantitySold();
		long price = sold == 0 ? offer.getPrice() : offer.getSpent() / sold;

		// The client re-sends every slot's state on login; skip exact repeats.
		String key = state + ":" + offer.getItemId() + ":" + sold + ":" + offer.getTotalQuantity() + ":" + price;
		if (key.equals(lastKey.get(slot)))
		{
			return;
		}
		lastKey.put(slot, key);
		Long placed = placedAt.get(slot);
		if (placed == null)
		{
			placed = now;
			placedAt.put(slot, placed);
		}

		Map<String, Object> ev = new LinkedHashMap<>();
		ev.put("t", now);
		ev.put("acc", account);
		ev.put("slot", slot);
		ev.put("b", buy);
		ev.put("id", offer.getItemId());
		ev.put("name", itemName);
		ev.put("qty", sold);
		ev.put("total", offer.getTotalQuantity());
		ev.put("price", price);
		ev.put("listed", offer.getPrice());
		ev.put("st", state.name());
		ev.put("placed", placed);

		boolean complete = state == GrandExchangeOfferState.BOUGHT
			|| state == GrandExchangeOfferState.SOLD
			|| state == GrandExchangeOfferState.CANCELLED_BUY
			|| state == GrandExchangeOfferState.CANCELLED_SELL;
		if (complete)
		{
			placedAt.remove(slot);
		}

		try
		{
			Files.createDirectories(file.getParent());
			Files.write(file, (gson.toJson(ev) + System.lineSeparator()).getBytes(StandardCharsets.UTF_8),
				StandardOpenOption.CREATE, StandardOpenOption.APPEND);
		}
		catch (IOException e)
		{
			log.warn("could not append trade to {}", file, e);
		}
	}
}
