package com.geanalyser;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Builds status.json for `ge-analyser execute` from what the client shows: GE offers,
 * coins and inventory. It only describes; nothing here can touch an offer.
 *
 * The engine issues order ids the game knows nothing about, so every offer gets a name
 * of its own (`ref`) the first time it is seen and keeps it until it leaves its slot,
 * plus `since`: when it was placed, if that was seen happening (0 = it was already
 * there). A finished offer stays in the file for a while after it has been collected,
 * marked `collected`, so its final count cannot be missed between two reads.
 */
class StatusWriter
{
	/** How long a finished offer stays listed after it left its slot. */
	static final long RETAIN_SECONDS = 600;

	/** One occupied GE slot as the client shows it. */
	static final class Offer
	{
		final int slot;
		final String state; // BUYING, BOUGHT, CANCELLED_BUY, SELLING, SOLD, CANCELLED_SELL
		final int itemId;
		final String name;
		final long price;
		final int total;
		final int sold;
		final long spent;

		Offer(int slot, String state, int itemId, String name, long price, int total, int sold, long spent)
		{
			this.slot = slot;
			this.state = state;
			this.itemId = itemId;
			this.name = name;
			this.price = price;
			this.total = total;
			this.sold = sold;
			this.spent = spent;
		}

		boolean buy()
		{
			return state.equals("BUYING") || state.equals("BOUGHT") || state.equals("CANCELLED_BUY");
		}

		boolean finished()
		{
			return !state.equals("BUYING") && !state.equals("SELLING");
		}

		private String signature()
		{
			return itemId + ":" + (buy() ? "b" : "s") + ":" + price + ":" + total;
		}
	}

	/** One inventory stack (noted and un-noted forms of an item share the item's id). */
	static final class Stack
	{
		final int itemId;
		final String name;
		final long qty;

		Stack(int itemId, String name, long qty)
		{
			this.itemId = itemId;
			this.name = name;
			this.qty = qty;
		}
	}

	private static final class Seen
	{
		String ref;
		long since;
		Offer offer;
		long goneAt;
	}

	private static final class Memory
	{
		final Map<Integer, Seen> slots = new TreeMap<>();
		final List<Seen> gone = new ArrayList<>();
	}

	private final Gson gson;
	/** What was seen of each account's offers. Kept apart, so logging into another account does not forget this one's. */
	private final Map<String, Memory> accounts = new HashMap<>();

	StatusWriter(Gson gson)
	{
		this.gson = gson.newBuilder().setPrettyPrinting().create();
	}

	/**
	 * Take up the offers of a status file this plugin wrote before, so that a client
	 * restart does not rename offers the engine already knows.
	 */
	void restore(String json)
	{
		try
		{
			JsonObject root = gson.fromJson(json, JsonObject.class);
			if (root == null || !root.has("source") || !"runelite".equals(root.get("source").getAsString()) || !root.has("orders"))
			{
				return;
			}
			Memory restored = new Memory();
			for (JsonElement el : root.getAsJsonArray("orders"))
			{
				JsonObject o = el.getAsJsonObject();
				if (o.has("collected") && o.get("collected").getAsBoolean())
				{
					continue;
				}
				Seen s = new Seen();
				s.ref = o.get("ref").getAsString();
				s.since = o.get("since").getAsLong();
				s.offer = new Offer(o.get("slot").getAsInt(), o.get("status").getAsString(), o.get("item_id").getAsInt(), o.get("item").getAsString(),
					o.get("price").getAsLong(), o.get("qty").getAsInt(), o.get("filled").getAsInt(), o.get("spent").getAsLong());
				restored.slots.put(s.offer.slot, s);
			}
			accounts.put(root.has("account") ? root.get("account").getAsString() : "", restored);
		}
		catch (RuntimeException e)
		{
			// Not a file of ours, or damaged: start without a memory.
		}
	}

	/**
	 * @param witnessed these slots were also looked at moments ago, so an offer that is
	 *                  new now was placed now; false for the first look after logging in
	 * @param offers    the occupied slots (empty slots left out)
	 * @return the text of status.json
	 */
	String update(long now, String account, boolean witnessed, List<Offer> offers, long cash, List<Stack> inventory, boolean members, int geSlots)
	{
		Memory memory = accounts.computeIfAbsent(account, a -> new Memory());
		Map<Integer, Seen> slots = memory.slots;
		List<Seen> gone = memory.gone;
		Set<Integer> occupied = new HashSet<>();
		for (Offer o : offers)
		{
			occupied.add(o.slot);
			Seen s = slots.get(o.slot);
			if (s != null && sameOffer(s.offer, o))
			{
				s.offer = o;
				continue;
			}
			if (s != null)
			{
				retire(gone, s, now);
			}
			Seen fresh = new Seen();
			fresh.ref = o.slot + "-" + now;
			fresh.since = witnessed ? now : 0;
			fresh.offer = o;
			slots.put(o.slot, fresh);
		}
		for (Iterator<Map.Entry<Integer, Seen>> it = slots.entrySet().iterator(); it.hasNext(); )
		{
			Map.Entry<Integer, Seen> e = it.next();
			if (!occupied.contains(e.getKey()))
			{
				retire(gone, e.getValue(), now);
				it.remove();
			}
		}
		gone.removeIf(s -> now - s.goneAt > RETAIN_SECONDS);

		Map<String, Object> root = new LinkedHashMap<>();
		root.put("source", "runelite");
		root.put("account", account);
		root.put("cash", cash);
		root.put("members", members);
		root.put("slots", geSlots);
		List<Map<String, Object>> orders = new ArrayList<>();
		for (Seen s : slots.values())
		{
			orders.add(entry(s, false));
		}
		for (Seen s : gone)
		{
			orders.add(entry(s, true));
		}
		root.put("orders", orders);
		List<Map<String, Object>> stacks = new ArrayList<>();
		for (Stack st : inventory)
		{
			Map<String, Object> m = new LinkedHashMap<>();
			m.put("item_id", st.itemId);
			m.put("item", st.name);
			m.put("qty", st.qty);
			stacks.add(m);
		}
		root.put("inventory", stacks);
		return gson.toJson(root);
	}

	/** The same offer as before: same item, side, price and size, no fewer units traded, and not running again after it had finished. */
	private static boolean sameOffer(Offer was, Offer is)
	{
		return was.signature().equals(is.signature()) && is.sold >= was.sold && !(was.finished() && !is.finished());
	}

	/** An offer left its slot. One that had finished is kept for a while with its final count; one that was still running ended unseen, and nothing is claimed about it. */
	private static void retire(List<Seen> gone, Seen s, long now)
	{
		if (s.offer.finished())
		{
			s.goneAt = now;
			gone.add(s);
		}
	}

	private static Map<String, Object> entry(Seen s, boolean collected)
	{
		Offer o = s.offer;
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("slot", o.slot);
		m.put("ref", s.ref);
		m.put("since", s.since);
		m.put("status", o.state);
		m.put("item_id", o.itemId);
		m.put("item", o.name);
		m.put("price", o.price);
		m.put("qty", o.total);
		m.put("filled", o.sold);
		m.put("spent", o.spent);
		if (collected)
		{
			m.put("collected", true);
		}
		return m;
	}
}
