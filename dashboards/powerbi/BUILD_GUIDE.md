# Power BI Dashboard — Build Guide

Assemble a 4-page report in Power BI Desktop from the CSVs exported by
`dashboards/powerbi/export.py`.

## Prerequisites

1. **Power BI Desktop** (free, Windows) — download from
   https://powerbi.microsoft.com/en-us/desktop/
2. Run the export once:
   ```powershell
   python -m dashboards.powerbi.export
   ```
   Creates 4 CSVs under `dashboards/powerbi/data/`.

## 1. Load the data

Power BI Desktop → **Home → Get Data → Text/CSV** → load all four:
- `listings.csv`
- `deals.csv`
- `localities.csv`
- `model_metrics.csv`

Click **Transform Data** to open Power Query. For each table, click
**Detect Data Type** (Home tab). Then **Close & Apply**.

## 2. Create relationships

**Model view** (left icon bar). Drag to create:
- `listings[locality_norm]` → `localities[locality_norm]` (Many-to-One)
- `deals[locality_norm]`    → `localities[locality_norm]` (Many-to-One)

## 3. Add DAX measures

Right-click the `listings` table → **New measure**. Paste each of these
(one measure per creation click):

```dax
Total Listings = COUNTROWS(listings)

Total Deals = CALCULATE(COUNTROWS(listings), listings[is_deal] = 1)

Deal Rate % =
DIVIDE([Total Deals], [Total Listings], 0) * 100

Median Rent = MEDIAN(listings[rent_monthly])

Median Save % on Deals =
CALCULATE(MEDIAN(listings[save_pct]), listings[is_deal] = 1)

Avg Rent per Sqft = AVERAGE(listings[rent_per_sqft])
```

## 4. Page-by-page assembly

### Page 1 — "Deals overview" (the money page)

**Top row: 4 KPI cards** (Insert → Card, one per metric):
- Total Listings
- Total Deals
- Deal Rate %
- Median Save % on Deals

**Middle: filterable deals table** (Insert → Table):
- Columns from `deals`: locality, bhk, area_sqft, rent_monthly, predicted_rent, save_pct, source_url
- Right-click `source_url` column → **Column format → Web URL**
- Sort by `save_pct` descending

**Right sidebar: Slicers** (Insert → Slicer, one per):
- `deals[locality]` (dropdown)
- `deals[bhk]` (list)
- `deals[property_type]` (list)
- `deals[rent_monthly]` (between slider)

### Page 2 — "Locality analysis"

**Left: Map** (Insert → Map or ArcGIS Map):
- Location: `localities[display_name]` (or Latitude/Longitude if you enable geo)
- Bubble size: `localities[n_deals]`
- Bubble color: `localities[median_rent_per_sqft]` (higher = premium red)

**Right: Bar chart** (Insert → Clustered Bar):
- Axis: `localities[display_name]`
- Values: `localities[median_rent]`
- Sort by median_rent descending

**Bottom: Scatter** (Insert → Scatter):
- X axis: `localities[median_rent_per_sqft]`
- Y axis: `localities[n_deals]`
- Size: `localities[n_listings]`
- Legend: `localities[nearest_metro_station]`

### Page 3 — "Market distribution"

**Top: Histogram of rent** (Insert → Histogram custom visual, or bin manually):
- Create calculated column `Rent Bucket` in `listings`:
  ```dax
  Rent Bucket =
  SWITCH(TRUE(),
      listings[rent_monthly] < 15000, "< 15k",
      listings[rent_monthly] < 25000, "15-25k",
      listings[rent_monthly] < 40000, "25-40k",
      listings[rent_monthly] < 60000, "40-60k",
      listings[rent_monthly] < 100000, "60-100k",
      "100k+"
  )
  ```
- Column chart: Axis = Rent Bucket, Value = Total Listings

**Middle: BHK × Locality heatmap** (Matrix visual):
- Rows: `listings[locality]`
- Columns: `listings[bhk]`
- Values: Median Rent
- Format: Conditional formatting → gradient by value

**Right: Property type breakdown** (Donut chart):
- Legend: `listings[property_type]`
- Value: Total Listings

### Page 4 — "Model transparency"

**Top: Metrics cards** (Card visual per row of `model_metrics`):
- Filter to `metric = "MAPE"`, show `value * 100` formatted as %
- Same for MAE, R², N training rows

**Middle: Predicted vs Actual scatter** (Scatter):
- X axis: `listings[predicted_rent]`
- Y axis: `listings[rent_monthly]`
- Add a diagonal reference line (`y = x`) via Analytics pane → Reference Line
- Points close to the diagonal = accurate; below = model over-predicted

**Right: Honest limitations panel** (Text box):
```
Model limitations
─────────────────
• Locality-level features only (no building/floor for 38% of listings)
• CV MAPE 22-23% — a 20% wobble on typical predictions
• NoBroker card errors pass through unchanged
• Rank-order deal detection compensates for point-estimate noise
• Verified: top-1 Domlur ₹15k 2BHK 1200sqft matches NoBroker exactly
```

## 5. Theme and polish

**View → Themes → Browse for themes** → pick a clean built-in (Executive works well).

**Format each visual:**
- Title on, subtitle off, keep font sizes ≥ 12
- Round rent values to nearest ₹1,000 (Format → Data label → Display units: Thousands)
- Currency prefix: `₹` (Custom format: `"₹"#,0`)

**Add slicer syncing** (View → Sync slicers) so page-level filters
propagate across pages 1-3.

## 6. Publish

- **Save as** `bengaluru-renter-copilot.pbix` in this folder
- **Publish** to Power BI Service (free tier is fine): File → Publish → My Workspace
- Get a shareable link from the Service. Add it to README.

## Refresh workflow

Whenever you re-scrape:
1. Run `python -m dashboards.powerbi.export`
2. In Power BI Desktop: **Home → Refresh** (pulls the updated CSVs)
3. Re-publish if you want the online version updated

For **auto-refresh** (Power BI Service), set up a Data Gateway or point
Power BI at the CSVs on a public URL (GitHub raw links work).
