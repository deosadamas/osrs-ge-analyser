package com.geanalyser;

import com.google.gson.annotations.SerializedName;
import java.util.List;
import java.util.Map;
import lombok.Data;

/** Mirror of the JSON written by `ge-analyser advise`. */
@Data
public class Advice
{
	private long ts;
	private long budget;
	private List<Flip> flips;
	private Map<String, Limit> limits;

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
	}

	@Data
	public static class Limit
	{
		private long used;
		private long limit;
		private Long resets;
	}
}
