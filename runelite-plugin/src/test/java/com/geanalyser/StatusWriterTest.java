package com.geanalyser;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.junit.Test;

public class StatusWriterTest
{
	private static final Gson GSON = new Gson();
	private static final int ORE = 440;
	private static final List<StatusWriter.Stack> NO_STACKS = Collections.emptyList();

	private static StatusWriter.Offer offer(int slot, String state, int price, int total, int sold)
	{
		return new StatusWriter.Offer(slot, state, ORE, "Iron ore", price, total, sold, (long) price * sold);
	}

	private static JsonArray orders(StatusWriter w, long now, boolean witnessed, StatusWriter.Offer... offers)
	{
		String json = w.update(now, "Zezima", witnessed, Arrays.asList(offers), 5_000, NO_STACKS, true, 8);
		return GSON.fromJson(json, JsonObject.class).getAsJsonArray("orders");
	}

	private static JsonObject only(JsonArray orders)
	{
		assertEquals(1, orders.size());
		return orders.get(0).getAsJsonObject();
	}

	@Test
	public void anOfferKeepsItsNameUntilItLeavesItsSlot()
	{
		StatusWriter w = new StatusWriter(GSON);
		// First look after logging in: the offer is there, nobody saw it being placed.
		JsonObject o = only(orders(w, 1000, false, offer(0, "BUYING", 44, 387, 0)));
		String ref = o.get("ref").getAsString();
		assertEquals(0, o.get("since").getAsLong());
		assertEquals("BUYING", o.get("status").getAsString());
		assertEquals(ORE, o.get("item_id").getAsInt());
		assertEquals(387, o.get("qty").getAsInt());

		o = only(orders(w, 1060, true, offer(0, "BUYING", 44, 387, 120)));
		assertEquals(ref, o.get("ref").getAsString());
		assertEquals(120, o.get("filled").getAsInt());
		assertEquals(44 * 120, o.get("spent").getAsLong());

		o = only(orders(w, 1120, true, offer(0, "BOUGHT", 44, 387, 387)));
		assertEquals(ref, o.get("ref").getAsString());
		assertFalse(o.has("collected"));

		// Collected: the slot is empty, but the final count stays listed for the engine to read.
		o = only(orders(w, 1180, true));
		assertEquals(ref, o.get("ref").getAsString());
		assertEquals("BOUGHT", o.get("status").getAsString());
		assertTrue(o.get("collected").getAsBoolean());

		// The slot is used again: a new offer with a new name, and this time it was seen being placed.
		JsonArray both = orders(w, 1240, true, offer(0, "SELLING", 46, 387, 0));
		assertEquals(2, both.size());
		JsonObject placed = both.get(0).getAsJsonObject();
		assertNotEquals(ref, placed.get("ref").getAsString());
		assertEquals(1240, placed.get("since").getAsLong());
		assertEquals(ref, both.get(1).getAsJsonObject().get("ref").getAsString());

		// ...and after the retention time the collected one is dropped.
		o = only(orders(w, 1180 + StatusWriter.RETAIN_SECONDS + 1, true, offer(0, "SELLING", 46, 387, 0)));
		assertEquals("SELLING", o.get("status").getAsString());
	}

	@Test
	public void aRunningOfferThatDisappearsIsNotReportedAsFinished()
	{
		StatusWriter w = new StatusWriter(GSON);
		orders(w, 1000, false, offer(2, "SELLING", 46, 100, 10));
		// It ended while nobody was looking (another client, a missed tick): its final count is unknown.
		assertEquals(0, orders(w, 1060, false).size());
	}

	@Test
	public void theSameOfferPlacedAgainIsANewOffer()
	{
		StatusWriter w = new StatusWriter(GSON);
		String first = only(orders(w, 1000, true, offer(1, "BOUGHT", 44, 100, 100))).get("ref").getAsString();
		// Same item, price and size in the same slot, but running again: it cannot be the finished one.
		JsonArray now = orders(w, 1060, true, offer(1, "BUYING", 44, 100, 0));
		assertEquals(2, now.size());
		assertNotEquals(first, now.get(0).getAsJsonObject().get("ref").getAsString());
		assertEquals(first, now.get(1).getAsJsonObject().get("ref").getAsString());
		assertTrue(now.get(1).getAsJsonObject().get("collected").getAsBoolean());
	}

	@Test
	public void namesSurviveAClientRestartButNotAnotherAccount()
	{
		StatusWriter before = new StatusWriter(GSON);
		orders(before, 1000, false, offer(0, "BUYING", 44, 387, 0));
		String written = before.update(1060, "Zezima", true, Arrays.asList(offer(0, "BUYING", 44, 387, 50), offer(3, "SELLING", 46, 20, 0)), 5_000, NO_STACKS, true, 8);
		JsonArray old = GSON.fromJson(written, JsonObject.class).getAsJsonArray("orders");

		StatusWriter after = new StatusWriter(GSON);
		after.restore(written);
		JsonArray now = orders(after, 5000, false, offer(0, "BUYING", 44, 387, 80), offer(3, "SELLING", 46, 20, 0));
		for (int i = 0; i < 2; i++)
		{
			assertEquals(old.get(i).getAsJsonObject().get("ref"), now.get(i).getAsJsonObject().get("ref"));
			assertEquals(old.get(i).getAsJsonObject().get("since"), now.get(i).getAsJsonObject().get("since"));
		}
		assertEquals(1060, now.get(1).getAsJsonObject().get("since").getAsLong());

		StatusWriter other = new StatusWriter(GSON);
		other.restore(written);
		String json = other.update(5000, "Someone else", false, Arrays.asList(offer(0, "BUYING", 44, 387, 80)), 1, NO_STACKS, false, 3);
		JsonObject root = GSON.fromJson(json, JsonObject.class);
		assertNotEquals(old.get(0).getAsJsonObject().get("ref"), only(root.getAsJsonArray("orders")).get("ref"));

		other.restore("{\"cash\": 10000, \"orders\": []}"); // a status typed by hand is not ours: ignored
		other.restore("not json");
		// Back on the first account: its offers still have their names.
		assertEquals(old.get(0).getAsJsonObject().get("ref"), orders(other, 5060, false, offer(0, "BUYING", 44, 387, 90)).get(0).getAsJsonObject().get("ref"));
	}

	@Test
	public void theFileSaysWhatTheEngineNeeds()
	{
		StatusWriter w = new StatusWriter(GSON);
		List<StatusWriter.Stack> stacks = Arrays.asList(new StatusWriter.Stack(ORE, "Iron ore", 387), new StatusWriter.Stack(8013, "Teleport to house", 3));
		JsonObject root = GSON.fromJson(w.update(1000, "Zezima", false, Collections.<StatusWriter.Offer>emptyList(), 12_345, stacks, false, 3), JsonObject.class);
		assertEquals("runelite", root.get("source").getAsString());
		assertEquals(12_345, root.get("cash").getAsLong());
		assertFalse(root.get("members").getAsBoolean());
		assertEquals(3, root.get("slots").getAsInt());
		assertEquals(0, root.getAsJsonArray("orders").size());
		JsonObject ore = root.getAsJsonArray("inventory").get(0).getAsJsonObject();
		assertEquals(ORE, ore.get("item_id").getAsInt());
		assertEquals(387, ore.get("qty").getAsLong());
		assertEquals(2, root.getAsJsonArray("inventory").size());
	}
}
