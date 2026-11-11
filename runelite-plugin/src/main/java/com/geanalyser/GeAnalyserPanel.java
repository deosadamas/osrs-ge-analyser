package com.geanalyser;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.Font;
import java.awt.GridLayout;
import java.text.NumberFormat;
import java.util.List;
import javax.swing.BorderFactory;
import javax.swing.Box;
import javax.swing.BoxLayout;
import javax.swing.JComponent;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTextArea;
import javax.swing.ScrollPaneConstants;
import javax.swing.SwingConstants;
import javax.swing.text.DefaultCaret;
import net.runelite.client.ui.ColorScheme;
import net.runelite.client.ui.FontManager;
import net.runelite.client.ui.PluginPanel;

/**
 * Next to the game: what `ge-analyser execute` says to do now while its loop is answering,
 * otherwise the flips `ge-analyser advise` suggests. It shows; it never clicks.
 */
class GeAnalyserPanel extends PluginPanel
{
	private static final NumberFormat GP = NumberFormat.getIntegerInstance();
	/** Where text wraps until its row has been laid out: a row's inner width with the scroll bar showing. */
	private static final int TEXT_WIDTH = 207;
	private static final String NOTHING = "Nothing to show yet.  Run `ge-analyser execute` for instructions, or `ge-analyser advise` for suggestions.";

	private final JLabel status = new JLabel();
	private final JLabel account = new JLabel();
	private final JPanel rows = new JPanel();

	// What the CLI last wrote; refresh() decides which of them is on screen.
	private Orders orders;
	private Advice advice;
	private int maxRows;
	private String note = NOTHING;
	private Object shown;

	GeAnalyserPanel()
	{
		super(false);
		setLayout(new BorderLayout());
		setBackground(ColorScheme.DARK_GRAY_COLOR);
		setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8));

		JLabel title = new JLabel("GE Analyser");
		title.setFont(FontManager.getRunescapeBoldFont());
		title.setForeground(Color.WHITE);

		status.setFont(FontManager.getRunescapeSmallFont());
		status.setForeground(ColorScheme.LIGHT_GRAY_COLOR);
		account.setFont(FontManager.getRunescapeSmallFont());
		account.setForeground(ColorScheme.LIGHT_GRAY_COLOR);

		JPanel header = new JPanel();
		header.setLayout(new BoxLayout(header, BoxLayout.Y_AXIS));
		header.setBackground(ColorScheme.DARK_GRAY_COLOR);
		header.setBorder(BorderFactory.createEmptyBorder(0, 0, 4, 0));
		header.add(title);
		header.add(status);
		header.add(account);
		add(header, BorderLayout.NORTH);

		rows.setLayout(new BoxLayout(rows, BoxLayout.Y_AXIS));
		rows.setBackground(ColorScheme.DARK_GRAY_COLOR);
		// The list scrolls up and down only: it is never wider than the panel, so long lines wrap or are cut short.
		JPanel list = new JPanel(new BorderLayout())
		{
			@Override
			public Dimension getPreferredSize()
			{
				return new Dimension(0, super.getPreferredSize().height);
			}
		};
		list.setBackground(ColorScheme.DARK_GRAY_COLOR);
		list.add(rows, BorderLayout.NORTH);
		JScrollPane scroll = new JScrollPane(list, ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED, ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER);
		scroll.setBorder(null);
		scroll.getVerticalScrollBar().setUnitIncrement(16);
		add(scroll, BorderLayout.CENTER);
		refresh(0);
	}

	/** The answer in orders.json, or null when there is no such file. */
	void setOrders(Orders orders)
	{
		this.orders = orders;
	}

	void setAdvice(Advice advice, int maxRows)
	{
		this.advice = advice;
		this.maxRows = maxRows;
	}

	/** There is no advice to show: its file is missing (null), or could not be read because of `problem`. */
	void setNoAdvice(String problem)
	{
		this.advice = null;
		this.note = problem == null ? NOTHING : problem;
	}

	/**
	 * The loop's answer is shown for as long as it is being kept. One that has gone stale
	 * gives way to the advice, and stays up (marked as old) only when there is none.
	 */
	static boolean showsOrders(Orders orders, boolean haveAdvice, long now)
	{
		return orders != null && (orders.isFresh(now) || !haveAdvice);
	}

	/** Called once a second: redraws the list when there is something new to show and keeps the ages current. */
	void refresh(long now)
	{
		boolean loop = showsOrders(orders, advice != null, now);
		Object model = loop ? orders : advice != null ? advice : note;
		if (model != shown)
		{
			shown = model;
			rows.removeAll();
			if (loop)
			{
				addOrders(orders);
			}
			else if (advice != null)
			{
				addAdvice(advice);
			}
			else
			{
				rows.add(message(note));
			}
			revalidate();
			repaint();
		}

		String more = "";
		if (loop)
		{
			boolean fresh = orders.isFresh(now);
			Orders.Account a = orders.getAccount();
			status.setText((a == null ? "execution loop" : "coins " + Orders.units(a.getCash())) + " · " + ageText(now - orders.getTs()) + " ago");
			status.setForeground(fresh ? ColorScheme.LIGHT_GRAY_COLOR : ColorScheme.PROGRESS_ERROR_COLOR);
			if (!fresh)
			{
				more = "<html>no new answer:<br>is `ge-analyser execute` running?</html>";
			}
			else if (a != null)
			{
				more = "<html>" + (a.getCashAfterActions() == a.getCash() ? "" : "after these actions: " + Orders.units(a.getCashAfterActions()) + " coins<br>")
					+ "equity ~" + gp(a.getEquityEst()) + " · realised " + (a.getRealised() > 0 ? "+" : "") + gp(a.getRealised())
					+ "<br>GE slots " + a.getGeSlotsUsed() + "/" + a.getGeSlots() + " · stacks " + a.getInventoryStacks() + "/" + a.getInventoryCapacity() + "</html>";
			}
		}
		else if (advice != null)
		{
			long age = now - advice.getTs();
			Advice.Agent agent = advice.getAgent();
			if (agent != null)
			{
				status.setText(String.format("agent equity %s · realised %s · %s ago", gp(agent.getEquity()), gp(agent.getRealised()), ageText(age)));
			}
			else
			{
				status.setText(String.format("budget %s · updated %s ago", gp(advice.getBudget()), ageText(age)));
			}
			status.setForeground(age > 600 ? ColorScheme.PROGRESS_ERROR_COLOR : ColorScheme.LIGHT_GRAY_COLOR);
		}
		else
		{
			status.setText("");
		}
		account.setText(more);
	}

	private void addOrders(Orders o)
	{
		if (o.getError() != null)
		{
			rows.add(message(o.getError()));
			return;
		}
		for (String w : o.getWarnings())
		{
			rows.add(card(line("! " + w, FontManager.getRunescapeSmallFont(), Color.YELLOW)));
			rows.add(Box.createVerticalStrut(4));
		}
		if (o.getActions().isEmpty())
		{
			rows.add(card(message("Nothing to do: no order, no stock, no flip that passes the filters.")));
		}
		for (Orders.Action a : o.getActions())
		{
			rows.add(row(a));
			rows.add(Box.createVerticalStrut(4));
		}
	}

	private void addAdvice(Advice advice)
	{
		List<Advice.Flip> flips = advice.getFlips();
		if (flips == null || flips.isEmpty())
		{
			rows.add(message("No flips pass the filters right now."));
			return;
		}
		int n = 0;
		for (Advice.Flip f : flips)
		{
			if (n++ >= maxRows)
			{
				break;
			}
			rows.add(row(f));
			rows.add(Box.createVerticalStrut(4));
		}
	}

	/** One instruction: what to do with which item, the offer to type in or look for, and why. */
	private static JPanel row(Orders.Action a)
	{
		JPanel p = card(line(a.getAction() + " " + a.getItem(), FontManager.getRunescapeBoldFont(), color(a)));
		if (a.isToDo())
		{
			p.add(line(a.detail(), FontManager.getRunescapeSmallFont(), Color.WHITE));
			if (a.needsCollectFirst())
			{
				p.add(line("take the items out of its buy offer first", FontManager.getRunescapeSmallFont(), Color.WHITE));
			}
		}
		if (a.getWhy() != null)
		{
			p.add(message(a.getWhy()));
		}
		return p;
	}

	private static Color color(Orders.Action a)
	{
		switch (a.getAction() == null ? "" : a.getAction())
		{
			case "BUY":
				return ColorScheme.PROGRESS_COMPLETE_COLOR;
			case "SELL":
				return ColorScheme.BRAND_ORANGE;
			case "CANCEL":
				return ColorScheme.PROGRESS_ERROR_COLOR;
			case "HOLD":
				return ColorScheme.LIGHT_GRAY_COLOR;
			default:
				return Color.WHITE; // COLLECT
		}
	}

	/** A block in the list: as wide as the panel, as tall as its lines. */
	private static JPanel card(JComponent... lines)
	{
		JPanel p = new JPanel()
		{
			@Override
			public Dimension getMaximumSize()
			{
				return new Dimension(Integer.MAX_VALUE, getPreferredSize().height);
			}
		};
		p.setLayout(new BoxLayout(p, BoxLayout.Y_AXIS));
		p.setBackground(ColorScheme.DARKER_GRAY_COLOR);
		p.setBorder(BorderFactory.createEmptyBorder(4, 6, 4, 6));
		for (JComponent l : lines)
		{
			p.add(l);
		}
		return p;
	}

	private static JTextArea message(String text)
	{
		return line(text, FontManager.getRunescapeSmallFont(), ColorScheme.LIGHT_GRAY_COLOR);
	}

	/** Text that wraps to the width it is given, breaking even a long file path, instead of running out of the panel. */
	private static JTextArea line(String text, Font font, Color color)
	{
		JTextArea t = new JTextArea();
		((DefaultCaret) t.getCaret()).setUpdatePolicy(DefaultCaret.NEVER_UPDATE); // a caret that follows the text scrolls the list to it
		t.setText(text);
		t.setFont(font);
		t.setForeground(color);
		t.setLineWrap(true);
		t.setWrapStyleWord(true);
		t.setEditable(false);
		t.setFocusable(false);
		t.setHighlighter(null);
		t.setOpaque(false);
		t.setBorder(null);
		t.setSize(TEXT_WIDTH, Short.MAX_VALUE); // its height follows from its width: this one, until the row is laid out
		return t;
	}

	private JPanel row(Advice.Flip f)
	{
		JPanel p = new JPanel(new GridLayout(3, 1, 0, 1));
		p.setBackground(ColorScheme.DARKER_GRAY_COLOR);
		p.setBorder(BorderFactory.createEmptyBorder(4, 6, 4, 6));
		p.setMaximumSize(new Dimension(Integer.MAX_VALUE, 56));

		String side = f.getSide() == null ? "flip" : f.getSide();
		boolean isBuy = side.equals("buy");
		boolean isSell = side.equals("sell");

		JLabel name = new JLabel((isBuy ? "BUY  " : isSell ? "SELL " : "") + f.getName());
		name.setFont(FontManager.getRunescapeBoldFont());
		name.setForeground(f.getLimitLeft() == 0 && f.getLimit() > 0 && !isSell ? ColorScheme.PROGRESS_ERROR_COLOR
			: isBuy ? ColorScheme.PROGRESS_COMPLETE_COLOR : isSell ? ColorScheme.BRAND_ORANGE : Color.WHITE);

		JLabel prices;
		JLabel detail;
		if (isBuy)
		{
			// Mirror of an open agent bid: place this exact offer.
			prices = new JLabel(String.format("%s × %s gp each", gp(f.getQty()), gp(f.getBuy())));
			String target = f.getSell() > 0 ? "then sell @ " + gp(f.getSell()) + " (+" + gp(f.getMargin()) + "/ea)" : "";
			String mkt = f.getMarket() == null ? "" : " · market " + gp(f.getMarket());
			detail = new JLabel(target + mkt + " · " + ageText(f.getAge()));
		}
		else if (isSell)
		{
			prices = new JLabel(String.format("%s × %s gp each", gp(f.getQty()), gp(f.getSell())));
			String mkt = f.getMarket() == null ? "" : " · market " + gp(f.getMarket());
			detail = new JLabel("cost " + gp(f.getBuy()) + " (+" + gp(f.getMargin()) + "/ea)" + mkt + " · " + ageText(f.getAge()));
		}
		else
		{
			prices = new JLabel(String.format("buy %s  →  sell %s   +%s/ea", gp(f.getBuy()), gp(f.getSell()), gp(f.getMargin())));
			String fill = f.getFill() == null ? "?" : Math.round(f.getFill() * 100) + "%";
			String limit = f.getLimit() > 0 ? gp(f.getLimitLeft()) + "/" + gp(f.getLimit()) : "-";
			detail = new JLabel(String.format("qty %s · limit %s · fill %s · exp %s", gp(f.getQty()), limit, fill, gp(f.getExpect())));
		}
		prices.setFont(FontManager.getRunescapeSmallFont());
		prices.setForeground(Color.WHITE);
		detail.setFont(FontManager.getRunescapeSmallFont());
		detail.setForeground(ColorScheme.LIGHT_GRAY_COLOR);
		detail.setHorizontalAlignment(SwingConstants.LEFT);

		p.add(name);
		p.add(prices);
		p.add(detail);
		return p;
	}

	private static String gp(long n)
	{
		if (Math.abs(n) >= 10_000_000)
		{
			return String.format("%.1fm", n / 1e6);
		}
		if (Math.abs(n) >= 100_000)
		{
			return String.format("%.0fk", n / 1e3);
		}
		return GP.format(n);
	}

	private static String ageText(long s)
	{
		if (s < 90)
		{
			return s + "s";
		}
		if (s < 5400)
		{
			return (s / 60) + "m";
		}
		return (s / 3600) + "h";
	}
}
