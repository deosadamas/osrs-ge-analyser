package com.geanalyser;

import com.google.gson.annotations.SerializedName;
import java.util.List;
import java.util.Map;
import lombok.Data;

/** Mirror of the JSON written by `ge-analyser advise` (ranking) or `advise --from-agent` (agent mirror). */
@Data
public class Advice
{
	private long ts;
	private long budget;
	private List<Flip> flips;
	private Map<String, Limit> limits;
	private Agent agent; // present only in --from-agent mode
	private List<Event> events;

	@Data
	public static class Flip
	{
		private int id;
		private String name;
		private long buy;
		private long sell;
		private long margin;
		private double roi;
		private long qty;
		private long limit;
		@SerializedName("limit_left")
		private long limitLeft;
		private long volume;
		private Double fill;
		private long expect;
		private long age;
		private String side; // "flip" (ranking), "buy" or "sell" (an agent order to mirror)
		private Long filled;
		private Long market;
	}

	@Data
	public static class Limit
	{
		private long used;
		private long limit;
		private Long resets;
	}

	@Data
	public static class Agent
	{
		private long equity;
		private long cash;
		private long realised;
		private long ticks;
		@SerializedName("last_tick")
		private long lastTick;
	}

	@Data
	public static class Event
	{
		private long t;
		private String msg;
	}
}
