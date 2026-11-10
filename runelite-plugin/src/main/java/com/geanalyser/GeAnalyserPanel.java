package com.geanalyser;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.GridLayout;
import java.text.NumberFormat;
import java.time.Instant;
import java.util.List;
import javax.swing.BorderFactory;
import javax.swing.BoxLayout;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.SwingConstants;
import net.runelite.client.ui.ColorScheme;
import net.runelite.client.ui.FontManager;
import net.runelite.client.ui.PluginPanel;

class GeAnalyserPanel extends PluginPanel
{
	private static final NumberFormat GP = NumberFormat.getIntegerInstance();

	private final JLabel status = new JLabel();
	private final JPanel rows = new JPanel();

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

		JPanel header = new JPanel(new GridLayout(2, 1));
		header.setBackground(ColorScheme.DARK_GRAY_COLOR);
		header.add(title);
		header.add(status);
		add(header, BorderLayout.NORTH);

		rows.setLayout(new BoxLayout(rows, BoxLayout.Y_AXIS));
		rows.setBackground(ColorScheme.DARK_GRAY_COLOR);
		add(rows, BorderLayout.CENTER);
		showMessage("Waiting for advice.json…  run `ge-analyser advise`");
	}

	void showMessage(String text)
	{
		rows.removeAll();
		JLabel l = new JLabel("<html><body style='width:180px'>" + text + "</body></html>");
		l.setForeground(ColorScheme.LIGHT_GRAY_COLOR);
		l.setFont(FontManager.getRunescapeSmallFont());
		rows.add(l);
		revalidate();
		repaint();
	}

	void show(Advice advice, int maxRows)
	{
		long age = Instant.now().getEpochSecond() - advice.getTs();
		status.setText(String.format("budget %s · updated %s ago", gp(advice.getBudget()), ageText(age)));
		status.setForeground(age > 600 ? ColorScheme.PROGRESS_ERROR_COLOR : ColorScheme.LIGHT_GRAY_COLOR);

		rows.removeAll();
		List<Advice.Flip> flips = advice.getFlips();
		if (flips == null || flips.isEmpty())
		{
			showMessage("No flips pass the filters right now.");
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
			rows.add(javax.swing.Box.createVerticalStrut(4));
		}
		revalidate();
		repaint();
	}

	private JPanel row(Advice.Flip f)
	{
		JPanel p = new JPanel(new GridLayout(3, 1, 0, 1));
		p.setBackground(ColorScheme.DARKER_GRAY_COLOR);
		p.setBorder(BorderFactory.createEmptyBorder(4, 6, 4, 6));
		p.setMaximumSize(new Dimension(Integer.MAX_VALUE, 56));

		JLabel name = new JLabel(f.getName());
		name.setFont(FontManager.getRunescapeBoldFont());
		name.setForeground(f.getLimitLeft() == 0 && f.getLimit() > 0 ? ColorScheme.PROGRESS_ERROR_COLOR : Color.WHITE);

		JLabel prices = new JLabel(String.format("buy %s  →  sell %s   +%s/ea", gp(f.getBuy()), gp(f.getSell()), gp(f.getMargin())));
		prices.setFont(FontManager.getRunescapeSmallFont());
		prices.setForeground(ColorScheme.BRAND_ORANGE);

		String fill = f.getFill() == null ? "?" : Math.round(f.getFill() * 100) + "%";
		String limit = f.getLimit() > 0 ? gp(f.getLimitLeft()) + "/" + gp(f.getLimit()) : "-";
		JLabel detail = new JLabel(String.format("qty %s · limit %s · fill %s · exp %s", gp(f.getQty()), limit, fill, gp(f.getExpect())));
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
