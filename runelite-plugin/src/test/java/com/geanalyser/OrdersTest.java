package com.geanalyser;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import com.google.gson.Gson;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.Test;

public class OrdersTest
{
	private static final Gson GSON = new Gson();

	/** OrdersTest.json is an answer `ge-analyser execute` wrote, with an action of every kind in it. */
	private static Orders written() throws IOException
	{
		try (Reader in = new InputStreamReader(OrdersTest.class.getResourceAsStream("OrdersTest.json"), StandardCharsets.UTF_8))
		{
			return GSON.fromJson(in, Orders.class);
		}
	}

	private static String read(Orders.Action a)
	{
		return a.getAction() + " " + a.getItem() + ", " + a.getSide() + " " + a.getFilled() + "/" + a.getQty() + " @ " + a.getPrice();
	}

	@Test
	public void theAnswerSaysWhatThePanelShows() throws IOException
	{
		Orders o = written();
		assertNull(o.getError());
		assertEquals(1794406844L, o.getTs());
		Orders.Account account = o.getAccount();
		assertEquals(8_858, account.getCash());
		assertEquals(1_715, account.getCashAfterActions());
		assertEquals(17_574, account.getEquityEst());
		assertEquals(900, account.getRealised());
		assertEquals(8, account.getGeSlotsUsed());
		assertEquals(8, account.getGeSlots());
		assertEquals(6, account.getInventoryStacks());
		assertEquals(26, account.getInventoryCapacity());
		assertEquals(1, o.getWarnings().size());

		List<Orders.Action> actions = o.getActions();
		assertEquals(12, actions.size());
		assertEquals("COLLECT Iron dart, sell 450/450 @ 7", read(actions.get(0)));
		assertEquals("COLLECT Death rune, sell null/null @ null", read(actions.get(1))); // an offer of your own: the engine knows the item, no more
		assertEquals("CANCEL Watermelon seed, buy 0/58 @ 50", read(actions.get(2)));
		assertEquals("no fill for 30m (0/58); free the slot and the coins", actions.get(2).getWhy());
		assertEquals("SELL Adamant dart, sell null/81 @ 25", read(actions.get(3)));
		assertTrue(actions.get(3).needsCollectFirst()); // the 81 are still in the buy offer that is filling
		assertEquals("BUY Iron dart, buy null/450 @ 5", read(actions.get(4)));
		assertFalse(actions.get(4).needsCollectFirst());
		assertEquals("HOLD Supercompost, inventory null/5 @ null", read(actions.get(11)));
		for (int i = 0; i < actions.size(); i++)
		{
			assertEquals(read(actions.get(i)), i < 7, actions.get(i).isToDo()); // the seven things to do come first
		}
	}

	@Test
	public void amountsToTypeInAreNeverShortened()
	{
		Orders.Action buy = GSON.fromJson("{'action': 'BUY', 'item': 'Feather', 'side': 'buy', 'qty': 123456, 'price': 1234567}", Orders.Action.class);
		assertTrue(buy.detail(), buy.detail().contains("123,456") && buy.detail().contains("1,234,567"));
	}

	@Test
	public void aSellOfEverythingYouHoldSaysSo()
	{
		Orders.Action sell = GSON.fromJson("{'action': 'SELL', 'item': 'Iron ore', 'side': 'sell', 'qty': 387, 'price': 46, 'all': true}", Orders.Action.class);
		assertEquals("all 387 × 46 gp each", sell.detail());
		Orders.Action part = GSON.fromJson("{'action': 'SELL', 'item': 'Iron ore', 'side': 'sell', 'qty': 387, 'price': 46}", Orders.Action.class);
		assertEquals("387 × 46 gp each", part.detail()); // you hold more than the engine sells: this many, typed in
	}

	@Test
	public void anAnswerIsShownWhileItIsKeptAndAnOldOneOnlyForWantOfAdvice()
	{
		Orders answer = GSON.fromJson("{'ts': 1000, 'error': 'no status.json yet', 'actions': []}", Orders.class);
		long kept = 1000 + Orders.FRESH_SECONDS;
		assertTrue(GeAnalyserPanel.showsOrders(answer, true, kept));
		assertFalse(GeAnalyserPanel.showsOrders(answer, true, kept + 1)); // the loop has stopped: back to the suggestions
		assertTrue(GeAnalyserPanel.showsOrders(answer, false, kept + 1)); // none to go back to: the old answer stays, marked old
		assertFalse(GeAnalyserPanel.showsOrders(null, false, kept));
	}
}
