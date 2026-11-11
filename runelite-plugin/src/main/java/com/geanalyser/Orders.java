package com.geanalyser;

import com.google.gson.annotations.SerializedName;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import lombok.Data;

/**
 * Mirror of the orders.json `ge-analyser execute` answers with: what to do at the GE now,
 * top to bottom. The plugin shows it and nothing more: every offer is placed, aborted and
 * collected by you.
 */
@Data
public class Orders
{
	/** The loop answers at least once a minute; an answer older than this is no longer being kept. */
	static final long FRESH_SECONDS = 180;

	private long ts;
	private String error; // instead of actions: why the status could not be used
	private Account account;
	private List<Action> actions;
	private List<String> warnings;

	/** An answer for a file that cannot be read: it says so where the actions would be. */
	static Orders unreadable(String why, long ts)
	{
		Orders o = new Orders();
		o.ts = ts;
		o.error = why;
		return o;
	}

	boolean isFresh(long now)
	{
		return now - ts <= FRESH_SECONDS;
	}

	public List<Action> getActions()
	{
		return actions == null ? Collections.<Action>emptyList() : actions;
	}

	public List<String> getWarnings()
	{
		return warnings == null ? Collections.<String>emptyList() : warnings;
	}

	/** A number as the game writes it. Never shortened: these are typed into the GE. */
	static String units(Long n)
	{
		return n == null ? "?" : String.format(Locale.US, "%,d", n);
	}

	@Data
	public static class Account
	{
		private long cash;
		@SerializedName("cash_after_actions")
		private long cashAfterActions;
		@SerializedName("equity_est")
		private long equityEst;
		private long realised;
		@SerializedName("ge_slots_used")
		private int geSlotsUsed;
		@SerializedName("ge_slots")
		private int geSlots;
		@SerializedName("inventory_stacks")
		private int inventoryStacks;
		@SerializedName("inventory_capacity")
		private int inventoryCapacity;
	}

	@Data
	public static class Action
	{
		private String action; // COLLECT, CANCEL, SELL, BUY or HOLD
		private String item;
		private String side; // buy or sell; inventory for a stack that is waiting to be sold
		private Long qty;
		private Long price;
		private Long filled;
		@SerializedName("collect_first")
		private List<Long> collectFirst;
		private Boolean all; // sells: the quantity is all you hold of the item
		private String why;

		/** Something to do at the GE, as opposed to an offer or a stack to leave alone. */
		boolean isToDo()
		{
			return !"HOLD".equals(action);
		}

		/** A sell of units that a running buy offer still holds: they have to be taken out of it first. */
		boolean needsCollectFirst()
		{
			return collectFirst != null && !collectFirst.isEmpty();
		}

		/** The offer to type in, or the one to collect or abort. */
		String detail()
		{
			if ("BUY".equals(action) || "SELL".equals(action))
			{
				// "all": every unit of it in your inventory, so the GE's All button sets the quantity.
				return (Boolean.TRUE.equals(all) ? "all " : "") + units(qty) + " × " + units(price) + " gp each";
			}
			String offer = side == null ? "offer" : side + " offer";
			if (qty == null || price == null)
			{
				return offer; // one this engine did not place: it only knows the item
			}
			return offer + " · " + units(filled == null ? 0 : filled) + "/" + units(qty) + " @ " + units(price) + " gp";
		}
	}
}
