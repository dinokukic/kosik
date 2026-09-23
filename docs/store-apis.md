# Store APIs — survey 2026-09-23

Live-verified (plain HTTP, no login) reference for building direct store connectors.
Compiled from three parallel surveys + a logged-in Makro inspection. Anything marked
unverified in a section was not confirmed with a live request.

**Decisions (2026-09-23):** direct connectors for Globus, Košík.cz, dm, Tesco, Billa,
Rossmann, Teta, Makro. Flyer-only via Kupi: Lidl, Albert, Kaufland, Penny.

| store | everyday prices | EAN | login | bot protection | verdict |
|---|---|---|---|---|---|
| Globus (globusonline.cz) | yes, GraphQL, batched | field + search | no | none (robots: 3 s delay) | **best** |
| Košík.cz | yes, full assortment, batched by SAP id | search (not a field) | no | none | worth it |
| Tesco (xapi.tesco.com) | yes, GraphQL, batched | `gtin` field, no EAN search | no (public web key, see .env `TESCO_XAPI_KEY`) | website Akamai 403; API fine | worth it |
| dm | yes, batched by id | field + search | no | search rate-limits (~5 req/s → 429) | worth it |
| Rossmann | yes (regular / club / kids-club) | field + search (verify match) | no | none | worth it |
| Teta | yes (shelf / promo / club, in haléře) | search only | no | none | worth it |
| Billa (billa.cz catalogue) | yes, batched by SKU (haléře) | none | no | none | worth it |
| Makro (sortiment.makro.cz) | yes — anonymous search = NET price per pack | yes | no (login only adds per-bundle detail) | www: Akamai; sortiment: none | **built** (`Makro`) |
| Lidl | partial (~500 food items) | none | no | 406 if Accept: json | flyer only (Kupi) |
| Albert | yes but search junk on multi-word, no pack size | none | no | none | flyer only (Kupi) |
| Kaufland | no (flyer only; own flyer JSON per store exists) | none | no | www 403 (marketplace) | flyer only (Kupi) |
| Penny | no (38 web promos) | none | no | none | flyer only (Kupi) |

## Makro (logged-in inspection, 2026-09-23)
- Anonymous `GET /searchdiscover/articlesearch/search?storeId=00005&language=cs-CZ&country=CZ&query=<EAN|name>&rows=N`
  → `results{variantId:{price, offers}}`. `price` = NET per the variant's smallest pack;
  × (1+VAT) equals the receipt exactly (butter 52.30→58.58, Pampers 369→446.49, Dove 21.50→26.02).
  A name search can hit a case-only variant (e.g. 1 275 Kč) — always price by the pack EAN.
- `GET /evaluate.article.v1/betty-variants?storeIds=00005&country=CZ&locale=cs-CZ&ids=<variantId>&ids=…`
  → bundles (packs) with description, `gtins[].number`, `bundleSize`; article `food` flag
  (NOT the VAT class — diapers are "food"). Logged in (+`customerId`, session cookie) each
  bundle's store entry adds `sellingPriceInfo{netPrice, grossPrice, finalPrice, vatPercent}`
  and promo fields (`promotionNetSalesPricePerPiece/Bundle`, `selectedFTEvaluatePromotions`).
  No promo was observed yet, so promo shape is unverified.
- Store 00005 = makro Praha – Černý Most (the user's store).


---

# Czech grocery e-shop price-API survey (A): Tesco, Albert, Billa, Globus

Tested live on 2026-09-23 from macOS with plain `curl` and Python `requests`, using a Chrome desktop User-Agent. I did not log in, set cookies or use a browser. Each store got about 10 to 25 requests. Anything not checked with a live request is marked **(unverified)**.

UA used everywhere: `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36`

---

## 1. Tesco (nakup.itesco.cz)

**Online shop:** Yes. It is a full online grocery shop with everyday prices. Prices look national. The API call below takes no store or postcode and still returns prices. Whether prices differ by delivery store is **(unverified)**.

**Website:** `https://nakup.itesco.cz/groceries/cs-CZ/...` returns **403 Access Denied from Akamai (AkamaiGHost, bot manager `bm_s` cookies)** for plain requests, on both search pages and product pages. `robots.txt` still loads. I did not try to get around this.

**API that works: Tesco's GraphQL gateway `https://xapi.tesco.com/`.** Plain POST requests work, with no cookies or login.

```
POST https://xapi.tesco.com/
Content-Type: application/json
x-apikey: $TESCO_XAPI_KEY   # public web-app key (the one Tesco's web front end uses; I could not read the CZ JS bundle because Akamai blocks it)
language: cs-CZ
region: CZ
User-Agent: <browser UA>
```
The body is a **JSON array of operations**, so several searches and product lookups can go in one POST. This is verified: 2 product lookups and 2 searches came back in one call.

**Search (name):**
```json
[{"operationName":"S","variables":{"query":"jihočeské máslo"},
  "query":"query S($query:String!){search(query:$query,page:1,count:3){results{node{... on ProductInterface{id gtin title brandName price{actual unitPrice unitOfMeasure} promotions{description attributes startDate endDate price{beforeDiscount afterDiscount}}}}}}}"}]
```
Response (trimmed):
```json
{"data":{"search":{"results":[
 {"node":{"id":"218209377","gtin":"08593803421216","title":"Madeta Jihočeské máslo 250g","brandName":"Madeta",
          "price":{"actual":34.9,"unitPrice":139.6,"unitOfMeasure":"kg"}}},
 {"node":{"id":"120895606","gtin":"08593803421605","title":"Madeta Jihočeské máslo 125g",
          "price":{"actual":34.9,"unitPrice":279.2,"unitOfMeasure":"kg"}}}]}}}
```
**Search by EAN:** `query:"8593803421216"` returns `results: []`, so search does not match on EAN.

**Price by id:**
```json
[{"operationName":"P","variables":{"tpnc":"218209377"},
  "query":"query P($tpnc:String){product(tpnc:$tpnc){id gtin title brandName isForSale price{actual unitPrice unitOfMeasure} promotions{id startDate endDate description unitSellingInfo price{beforeDiscount afterDiscount} attributes}}}"}]
```
```json
{"data":{"product":{"id":"218209377","gtin":"08593803421216","title":"Madeta Jihočeské máslo 250g","isForSale":true,
  "price":{"actual":34.9,"unitPrice":139.6,"unitOfMeasure":"kg"},
  "promotions":[{"startDate":"2026-09-22T22:00:00Z","endDate":"2026-09-29T22:00:00Z",
     "description":"-36%, předtím 54,90 Kč","price":{"beforeDiscount":54.9,"afterDiscount":34.9},"attributes":[]}]}}}
```
To batch, put one `product(tpnc:…)` operation per id in the array.

**Price semantics (verified):**
- `price.actual` is the **current selling price, including VAT and any normal promotion**. During a promo it is the promo price.
- The regular shelf price is `promotions[].price.beforeDiscount` (54.90 above). With no promotion, `actual` is the regular price. Example: Magnesia 1.5 l at 18.50 with `promotions: []`.
- **Clubcard prices** show up as a promotion with `attributes:["CLUBCARD_PRICING"]`. `actual` stays at the shelf price, and the Clubcard price appears only inside `description`, e.g. `"119,90 Kč Ušetřete 1/3 s Clubcard"`, where `afterDiscount` = shelf price. You need to parse that text.
- `unitPrice` + `unitOfMeasure` (kg / l / kus / m). The pack size is only in `title`.

**EAN:** Yes. Every product has `gtin` (14-digit, zero-padded, e.g. `08593803421216`). Search by EAN does not work, so match by name once and store the tpnc↔gtin mapping. Verified: Pampers Premium Care 1 72 ks = id `105003226`, gtin `08006540858073`, 369.90.

**Login/bot:** No login needed on xapi. The website itself is behind Akamai and blocks plain requests.

**Verdict: direct connector worth it.** A batched GraphQL endpoint with no auth returns price, unit price, promo and regular price, and GTIN. The risk is that the API key or schema is undocumented and could change.

---

## 2. Albert (albert.cz)

**Online shop:** Yes, at `https://www.albert.cz/shop/...`, product URLs `/shop/<cat>/.../p/<code>`. This is Ahold Delhaize's Next.js and Hybris platform, and the JS includes Cart, Checkout, EcomRegions and DeliveryTypes queries. The catalogue is broad: Magnesia, Pampers and Harmony are all present. The delivery area and whether prices vary by region are **(unverified)**. Anonymous API calls return one price set with no store context.

**API:** Apollo GraphQL at `https://www.albert.cz/api/v1/`. The web client uses persisted queries, but **plain full-text POST queries are accepted** (verified). Introspection is disabled. No cookies are needed.

```
POST https://www.albert.cz/api/v1/
Content-Type: application/json
User-Agent: <browser UA>
Origin: https://www.albert.cz        (sent; necessity not tested)
x-apollo-operation-name: GetProductSearch   (sent; necessity not tested)
```

**Search:**
```json
{"operationName":"GetProductSearch",
 "variables":{"lang":"cs","searchQuery":"jihočeské máslo","pageSize":3,"pageNumber":0},
 "query":"query GetProductSearch($lang:String $searchQuery:String $pageSize:Int $pageNumber:Int){productSearch:productSearchV2(lang:$lang searchQuery:$searchQuery pageSize:$pageSize pageNumber:$pageNumber){products{...P} pagination{totalResults}}} fragment P on Product{code name manufacturerName available price{formattedValue value priceType unit unitCode unitPrice unitPriceFormatted wasPrice showStrikethroughPrice discountedPriceFormatted supplementaryPriceLabel1} potentialPromotions{code title description promotionType toDisplay endDate} url isLowPriceGuarantee isPermanentPriceReduction}"}
```
```json
{"data":{"productSearch":{"products":[
 {"code":"20976781","name":"Madeta Jihočeské máslo","manufacturerName":"MADETA","available":true,
  "price":{"formattedValue":"54,90 Kč","value":54.9,"priceType":"BUY","unit":"piece","unitCode":"pieces","unitPrice":54.9,"wasPrice":null,"showStrikethroughPrice":false},
  "potentialPromotions":[],"url":"/shop/Mlecne-a-chlazene/Maslo-tuky-margariny/Maslo/Madeta-Jihoceske-maslo/p/20976781"},
 {"code":"20297480","name":"Madeta Jihočeské máslo", "price":{"value":34.9, ...}}],
 "pagination":{"totalResults":75}}}}
```
Notes:
- Search quality is poor for multi-word queries. `"magnesia perlivá 1,5"` and `"pampers premium care 1"` returned milk and bananas. Single words (`magnesia`, `pampers`, `harmony`) work.
- **Pack size is missing:** product names do not include the size. The two "Madeta Jihočeské máslo" results above (54.90 and 34.90) are presumably 250 g and 125 g, but nothing in the response says which **(inferred)**. `unitPrice` is per piece for packaged goods and per kg only for weighed goods.
- Promotions: across about 70 products checked, `wasPrice` was always null and `potentialPromotions` empty. Anonymous prices may be plain shelf prices, or promo and Albert-card prices may need a session or segment. How promos are represented is **(unverified)**.

**Price by ids (batched), verified:**
```json
{"operationName":"GetProductsDetails",
 "variables":{"productCodes":["20976781","20297480"],"lang":"cs","inStockOnly":false},
 "query":"query GetProductsDetails($productCodes:[String!]! $lang:String! $inStockOnly:Boolean!){products(productIds:$productCodes lang:$lang inStockOnly:$inStockOnly){products{...P}}} fragment P on Product{...same as above...}"}
```
For a single product, `productDetails(productCode:"20976781", lang:"cs")` also returns `description` and `mobileClassificationAttributes` (brand, manufacturer, origin; oddly, internal sales figures as well). Neither includes weight or EAN.

**EAN:** **No.** Requesting `ean` or `gtin` fails with *Cannot query field "ean" on type "Product"*. EAN search returns 0 results. The product HTML page redirects to the homepage for a cookie-less request, so I could not check its JSON-LD.

**Login/bot:** No login, no bot protection seen. Plain requests work.

**Verdict: possible but painful.** Prices are easy to get by code, but there is no EAN, no pack size and weak search, so mapping has to be done by hand. Promo and loyalty handling is unclear.

---

## 3. Billa (billa.cz)

**Online shop:** `shop.billa.cz` redirects to `https://www.billa.cz/online-nakup`, which sends you to **partners (Wolt, foodora)**. Billa CZ has no checkout of its own. However, **www.billa.cz has a full product catalogue with everyday prices** (`/produkty/...`, `/produkt/<slug>`). It runs on the same platform as shop.billa.at (Nuxt + commercetools). Prices come without a store or region parameter, so they look national.

**API:** REST JSON at `https://www.billa.cz/api/product-discovery/...`. Plain GET with just a User-Agent works. No cookies, no login.

**Search:**
```
GET https://www.billa.cz/api/product-discovery/products/search/{urlencoded term}?pageSize=4
Accept: application/json
```
```json
{"count":3,"offset":0,"total":7,"results":[
 {"sku":"82-204112","slug":"madeta-jihoceske-maslo-250g-82204112","name":"Madeta Jihočeské máslo 250g",
  "brand":{"name":"Madeta"},"amount":"250","volumeLabelShort":"g","weight":0.25,"packageLabelKey":"ks","inPromotion":false,
  "price":{"baseUnitShort":"kg","regular":{"value":4490,"perStandardizedQuantity":17960,"tags":[]}}},
 {"sku":"82-209497","name":"Madeta Jihočeské máslo 125g","price":{"regular":{"value":3690,"perStandardizedQuantity":29520}}}]}
```
Promo item (verified):
```json
{"sku":"82-326348","name":"Pampers Premium Care Velikost 2, 68 Plenek, 4kg-8kg","inPromotion":true,
 "price":{"baseUnitShort":"ks","crossed":39900,"discountPercentage":-12,
   "regular":{"value":34900,"perStandardizedQuantity":513,"promotionValue":39900,"promotionBasePrice":587,"tags":["pt-aktion"]},
   "standard":{"value":39900,"perStandardizedQuantity":587},"lowestPrice":34900}}
```
**Price by ids (batched), verified:**
```
GET https://www.billa.cz/api/product-discovery/products?skus=82-204112&skus=82-249392&skus=82-326348&pageSize=10
```
This returns the same objects as search. A single product is `GET /api/product-discovery/products/82-204112` (by SKU; by slug returns 404). It adds ingredients, nutrition and manufacturer details.

**Price semantics:**
- All prices are in **haléře** (integer ÷ 100 = CZK) and include VAT.
- `regular.value` is the **current** price, which during a promo is the promo price, despite the field name.
- The everyday shelf price is `standard.value`, or `crossed`/`regular.promotionValue`, when `inPromotion` is true. Otherwise it is `regular.value`.
- `perStandardizedQuantity` is the unit price per `baseUnitShort`. The pack size is structured: `amount`, `volumeLabelShort`, `weight`.
- Promo type is in `tags` (`pt-aktion`). BILLA Bonus loyalty-only prices were not seen; representation **(unverified)**.

**EAN:** **No.** There is no EAN field in search or detail JSON. EAN search (`/search/8594001021666`) returns total 0. Product-page JSON-LD has only Corporation and BreadcrumbList, no Product/gtin.

**Login/bot:** None. Plain requests work.

**Verdict: direct connector worth it.** It is a clean REST API with batch lookup by SKU, structured pack size, and regular vs promo prices. The only gap is EAN, so each household item needs a one-time SKU mapping by name.

---

## 4. Globus (globusonline.cz, linked from globus.cz)

**Online shop:** Yes, **globusonline.cz** is a full online grocery shop on the Shopsys platform. Separately, `www.globus.cz/<store>/hypermarket/cela-nabidka/` has per-store assortment pages, including Praha-Zličín, Čakovice and Štěrboholy. Its robots.txt disallows the product (`/p/`) and sub-category paths, so I did not scrape it. globusonline delivery coverage for Prague 1 and any regional pricing are **(unverified)**. Anonymous API calls return one price set.

**API:** Shopsys Frontend API GraphQL at `https://globusonline.cz/graphql/`. The URL is published in the page's `window.__ENV.publicGraphqlEndpoint`. Plain POST works with no cookies or login. Introspection is disabled, but the field names come from the site's own JS fragments (`ListedProductFragment`, `ProductPriceFragment`).

```
POST https://globusonline.cz/graphql/
Content-Type: application/json
User-Agent: <browser UA>
```
**Search (name *or EAN*):**
```json
{"query":"query S($q:String!){productsSearch(searchInput:{search:$q,isAutocomplete:false,userIdentifier:\"00000000-0000-4000-8000-000000000000\"},first:3){totalCount edges{node{uuid name catalogNumber ean sellUnitSizeText isAvailableForDelivery flags{name} price{...PP} registeredUserPrice{...PP}}}}} fragment PP on ProductPrice{priceWithVat priceWithoutVat vatAmount percentageDiscount basicPrice{priceWithVat} globusOriginalPrice globusDiscountPercentage priority nextPriceChange baseComparisonPrice baseComparisonSaleUnitSizeText}",
 "variables":{"q":"8593803421216"}}
```
```json
{"data":{"productsSearch":{"totalCount":1,"edges":[{"node":{
  "uuid":"3fcefc68-8b8f-4ac5-9dc1-e9194325fd4b","name":"Madeta Jihočeské máslo 250 g",
  "catalogNumber":"00072764008","ean":"8593803421216","sellUnitSizeText":"250 g","flags":[{"name":"ČESKÁ POTRAVINA"}],
  "price":{"priceWithVat":"54.900000","priceWithoutVat":"49.020000","vatAmount":"5.880000","basicPrice":{"priceWithVat":"54.900000"},
           "globusOriginalPrice":null,"globusDiscountPercentage":null,"priority":"DEFAULT_PRICE",
           "baseComparisonPrice":"219.600000","baseComparisonSaleUnitSizeText":"1 kg"},
  "registeredUserPrice":{"priceWithVat":"0.000000","priority":"REGISTERED_USER_PRICE"}}}]}}}
```
Searching by EAN found the exact item for butter, Pampers PC1 72 ks (`8006540858073`, 379.90) and **Harmony 4vrstvý 16 ks (`8584014858502`)**. Search is fuzzy, though: Magnesia `8594001021666` returned an unrelated Granini with a similar EAN, so the code must **check that `ean` matches exactly**. By name, Magnesia Perlivá 1.5 l comes back as EAN `8594001021642` at 14.90. The household-list EAN may be a different SKU or pack.

**Price by ids (batched), verified:**
```json
{"query":"query($c:[String!]!){productsByCatnums(catnums:$c){uuid name catalogNumber ean price{priceWithVat globusOriginalPrice priority}}}",
 "variables":{"c":["00072764008","00809338007"]}}
```
This returns both products (250 g at 54.90, 125 g at 35.90).

**Price semantics (verified):**
- `price.priceWithVat` is the current price. `priceWithoutVat` and `vatAmount` are also given. Values are decimal strings.
- **Promo:** `priority:"SPECIAL_PRICE"` with `globusOriginalPrice` (the regular price) and `globusDiscountPercentage`. Example: Cottage 200 g 16.90, original 22.90, 26 %.
- `SPECIAL_PRICE` with `globusOriginalPrice:null` appears on "CENOVÁ JISTOTA" (price-guarantee) items such as Magnesia. Treat that as the everyday price **(inferred)**.
- `DEFAULT_PRICE` is the plain shelf price.
- `registeredUserPrice` (the Můj Globus member price) is `0` for anonymous requests. It probably needs login **(inferred)**.
- The unit price is `baseComparisonPrice` per `baseComparisonSaleUnitSizeText`. The pack size is `sellUnitSizeText`.

**EAN:** **Yes.** There is an `ean` field on every product, and EAN works as a search term (with an exact-match check).

**Login/bot:** None for the API. robots.txt asks for `Crawl-delay: 3`. It disallows `/hledani` (the HTML search page) but not `/graphql/`.

**Verdict: direct connector worth it.** It is the best of the four: EAN lookup and field, batch by catalogue number, VAT split, and explicit promo and original price.

---

## Summary

| Store | Everyday prices online | EAN | Login needed | Bot protection | Verdict |
|---|---|---|---|---|---|
| Tesco | Yes, full shop; `xapi.tesco.com` GraphQL, batched | In JSON (`gtin`); EAN search does not work | No (public web x-apikey) | Website: Akamai 403 for plain requests. xapi: none seen | **Direct connector worth it** |
| Albert | Yes, `/shop`; `albert.cz/api/v1/` GraphQL, batched by code | No | No | None seen | **Possible but painful** (no EAN or pack size, weak search, promos unclear) |
| Billa | Catalogue with prices on billa.cz (checkout is via Wolt/foodora); REST, batched by SKU | No | No | None seen | **Direct connector worth it** (one-time SKU mapping) |
| Globus | Yes, globusonline.cz; Shopsys GraphQL, batched by catnum | Yes: field and search | No (member price needs login) | None seen | **Direct connector worth it** (best of the four) |

Scratch scripts in this folder: `tesco.py`, `tesco_batch.py`, `albert.py`, `billa.py`, `globus.py`, `globus_q.py`, plus the raw responses.

---

# Survey B: Košík, Kaufland, Lidl, Penny (live-tested 2026-09-23)

All requests were plain `curl`/`requests` calls with a desktop Chrome User-Agent, no cookies, no login, about 1 request per second, and fewer than 30 requests per store. Anything not tested live is marked **(unverified)**.

UA used: `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36`

---

## 1. Košík.cz: full online grocer with a clean JSON API

**Everyday prices:** yes. The whole assortment is listed with shelf prices. Promos appear inline on the same items. No login or cookie is needed: every call below worked cold, and the server only sets a harmless `_nss=1` cookie. There was no Cloudflare or captcha. Delivery is region-based, but I did not test whether prices change by delivery address **(unverified)**. Stock and availability are per day.

Base URL: `https://www.kosik.cz/api/front/` (found in the `api.*.js` bundle, `frontApiBaseUrl`). Send `Accept: application/json`.

### Search (name or EAN) → ids
`GET https://www.kosik.cz/api/front/suggest/v2?query=<q>&limit=<n>` (optional `vendor`, `deliverToday`)

```json
{"products":{"items":[{"id":12677,"name":"Madeta Jihočeské máslo (82%)","brand":{"id":402,"name":"Madeta"},
  "url":"/p12677-madeta-jihoceske-maslo-82","price":59.9,"recommendedPrice":59.9,"percentageDiscount":0,
  "isSale":false,"unit":"ks","productQuantity":{"value":250.0,"unit":"g"},
  "pricePerUnit":{"price":239.6,"unit":"kg"},"loyaltyClubBenefitsPrice":null,
  "hasLoyaltyClubBenefitsPrice":false,"returnablePackagePrice":0.0,"availability":[{"date":"2026-09-23","quantity":100}]}]},
 "categories":…,"brands":…,"otherProducts":{…},"dataSourceEndpoint":"autocomplete"}
```

- **Searching by EAN works** (the query is the bare EAN):
  - `8593803421216` → 12677 Madeta Jihočeské máslo 250 g, 59.90
  - `8006540858073` → 725142 Pampers Premium Care vel. 1, 72 ks, 499.00
  - `8594001021666` → 476093 "Magnesia Přírodní perlivá **multipack 6×1,5l**", 114.90. Košík maps the single-bottle EAN to the multipack. The single 1.5 l bottle is not sold. A check on pack size is needed.
  - `4014400762235` (Toffifee) → no hit. A name search for "toffifee" returns 29027 Toffifee bonboniéra 125 g at 57.90, so this EAN is probably not the one Košík stores.
- `GET /api/front/product/search?...` exists but is category/slug-scoped ("Slug is empty"). It is not needed.

### Price by id
- Single product: `GET https://www.kosik.cz/api/front/product/<id>` → `{"product":{…same fields as above…, "detail":{"sapId":"1001141", …}}, "breadcrumbs":…}`
- **Batch** (verified): `GET https://www.kosik.cz/api/front/products/sapid?sap_ids[]=1001141&sap_ids[]=1073267` → `{"products":[{id:12677,…price…},{id:725142,…}],"notFoundIds":[]}`. The key is the SAP id from `product.detail.sapId`, so resolve that once per item and store it.
- Price fields:
  - `price` is what you pay now.
  - `recommendedPrice` is the regular price.
  - A promo shows when `price < recommendedPrice`, together with `percentageDiscount`, `actionLabel` ("Akce platí do 29. 9."), and `priceLabels`. `isSale` stayed `false` even on a -21 % item, so **do not rely on `isSale`**.
  - `loyaltyClubBenefitsPrice` is Košík's club price. It was null for every item tested.
  - `cumulativePrices` holds multi-buy prices.
  - `pricePerUnit` gives `{price, unit}`. `productQuantity` gives the pack size.
- VAT: consumer prices include VAT. This is inferred from the site being B2C, since there is no explicit field.

### EAN
- Searchable in `suggest/v2`.
- **Not present** as a field in the product JSON. Neither `detail` nor the top-level fields contain it.

**Verdict: direct connector worth it.** It has a free JSON API with EAN search, a batched price call, regular and promo prices, and no bot wall.

---

## 2. Kaufland: flyer only (belief confirmed), but the flyer is clean structured JSON

- `www.kaufland.cz` is **Kaufland Marketplace** (Kaufland Marketplace GmbH, mostly non-food third-party sellers). It returned a **Cloudflare 403 "Přístup blokován"** to plain curl, so I stopped there.
- The grocery chain's site is **`prodejny.kaufland.cz`** (Adobe AEM). There was no bot wall and no cookie was needed.

**Everyday prices:** **no.** The `/sortiment/*` pages are marketing and brand pages with no prices. Site search (`/vyhledat.html?q=`) returns only current offers (component `OfferSearch`). About 811 offers exist per store per week. Offers are **store-specific**: the offer id embeds the store, e.g. `…CZ3300…`. Geo-IP assigned CZ5810 Praha-Palmovka, and the default is CZ3300 Praha-Vypich.

### Full flyer for a store (one request)
`GET https://prodejny.kaufland.cz/nabidka/prehled.storeName=CZ5810.html`. The AEM selector sets the store; this was verified by the store id switching in the output. The page is about 2.8 MB of HTML with an embedded `window.SSR['<uuid>'] = {...}` JSON blob. Parse it with `json.JSONDecoder().raw_decode` starting right after `= `:

```json
{"component":"OfferTemplate","props":{"weekData":{"currentWeekDates":["2026-09-23",…]},
 "offerData":{"cycles":[{"categories":[{"displayName":"Ovoce, zelenina, rostliny","offers":[
  {"offerId":"20260923.02702396.CZ3300.CZ260923","klNr":"02702396","title":"Toffifee",
   "subtitle":"Lískové ořechy v karamelu","price":34.9,"formattedOldPrice":"47,90","discount":27,
   "unit":"125 g","basePrice":"(=100 g 27,92)","dateFrom":"2026-09-23","dateTo":"2026-09-29",
   "listImage":"https://kaufland.media.schwarz/is/image/schwarz/4014400400007_CZ_P","loyaltyDiscount":0}]}]}]}}}
```

Kaufland Card offers (49 this week) add fields such as the following:

```json
"detailTitle":"Tvoje cena s\nKaufland Card XTRA","price":14.9,"formattedOldPrice":"25,90",
"loyaltyDiscount":50,"loyaltyFormattedPrice":"12,90","loyaltyBasePrice":"(=100 g 8,60/7,38)",
"customerType":"KDN","bonusbuy":true
```

- **`formattedOldPrice` is the regular shelf price**, but only for items that are on promo. This is a useful by-product.
- Other endpoints:
  - `/.kloffers.storeName=CZ3300.json` returns only `[{klNr,dateFrom,dateTo}]` with no prices.
  - Store info: `/.klstorebygeo.json` (geo) and `/.klstorebygeo.storeName=CZ3300.json`.
  - Search: `GET https://prodejny.kaufland.cz/vyhledat.html?q=<q>` → same `window.SSR` blob with `component:"OfferSearch"`, `props.offerData[]`. Tested:
    - "máslo" → K-Bio máslo 59.9 (was 69.90); Tatra máslo 29.9 (was 52.90, 23–24 Sep); Madeta not on offer.
    - "toffifee" → 34.9 (was 47.90).
    - EAN `8593803421216` → no results.
- **EAN:** there is no field for it. The `listImage` file name often *is* an EAN, for example `4014400400007_CZ_P` or `8594001021642_CZ_P` for Magnesia "různé druhy". Sometimes it is an internal number instead (`1145_CZ_P`, `2815470000000`). This makes it a heuristic only.
- Prices on a Czech retail flyer include VAT (inferred).

**Verdict: no, flyer only, so rely on Kupi.** Kaufland's own flyer JSON is complete, store-specific, and carries the Card price and the regular "old" price. It could replace Kupi for Kaufland if you want the Card and regular price split.

---

## 3. Lidl: belief partly refuted. Some everyday food prices are online, with a batch API.

The online shop sells non-food. The **search API also returns in-store food items with prices**: about 505 "Potraviny a nápoje" items this week. They fall into two groups by badge:
- **Dated flyer items:** badge `"Pouze v prodejnách od 24.09. - 27.09."` or `"V prodejnách pouze 21.09. - 23.09."`. About half of these have a strike-through price.
- **Undated items:** badge `"Pouze v prodejnách"` with no dates and no `oldPrice`. These look like **permanent-assortment shelf prices**. Examples: `Madeta Jihočeské máslo 82%` 250 g at 46.90, `Mattoni Magnesia neperlivá` 1.5 l at 15.90 (with a 6+ multi-buy note), `Trvanlivé mléko 1,5%` at 12.90.

The permanent assortment appears only partly: Toffifee, Pampers and Magnesia *perlivá* are not found. Coverage is **incomplete (inferred)**. Prices are national, with no store or region parameter (`regionsPrices:{}`). There was no bot protection, no cookie and no login.

### Search → ids
`GET https://www.lidl.cz/q/api/search?q=<q>&assortment=CZ&locale=cs_CZ&version=v2.0.0&fetchsize=48&offset=0`
- Optional filter: `&category=Potraviny+a+n%C3%A1poje`. `q=*` with that filter gave numFound 505. `maxfetchsize` is 1000, but only 60 items came back per call.
- **Do not send `Accept: application/json`**, because that returns 406. The response content-type is `application/mindshift.search+json;version=2`.
- robots.txt disallows `*search?q=*`. This is a personal, low-volume call, but note it.

```json
{"numFound":57,"items":[{"code":"10054149","gridbox":{"data":{
  "erpNumber":"10054149","fullTitle":"Madeta Jihočeské máslo 82%","category":"Food",
  "canonicalUrl":"/p/madeta-jihoceske-maslo-82/p10054149","ians":["6707164"],
  "price":{"price":46.9,"oldPrice":0.0,"basePrice":{"text":"250 g, 100 g = 18,96 Kč"},"currencyCode":"CZK"},
  "lidlPlus":[],
  "stockAvailability":{"badgeInfo":{"badges":[{"text":"Pouze v prodejnách","type":"IN_STORE"}]},"onlineAvailable":false},
  "disclaimers":[{"value":"Basic"},{"value":"StrikePrice_LowestPrice30days"}]}}}]}
```

### Price by id (batch, verified)
`GET https://www.lidl.cz/p/api/gridboxes/CZ/cs?erpNumbers=10054149,10054121` → a JSON array of the same `gridbox.data` objects:

```
10054121 Mattoni Magnesia Miner. voda neperlivá 15.9 oldPrice 0.0 "1,5 l, 1 l = 10,60 Kč" ['Pouze v prodejnách']
10054149 Madeta Jihočeské máslo 82%             46.9 oldPrice 0.0 "250 g, 100 g = 18,96 Kč" ['Pouze v prodejnách']
```

Field meanings:
- `price.price` is the current price.
- A promo carries `price.discount.{deletedPrice, percentageDiscount}` plus `oldPrice`.
- **Lidl Plus** prices sit in `lidlPlus[0].price.{price, oldPrice, discount}` with `lidlPlusText:"s Lidl Plus"`. When that is present, the top-level `price.price` can be missing (example: CHEF SELECT listové těsto, 31.90 with Lidl Plus, 39.90 regular).
- Unit and pack size come only as text in `basePrice.text` or `packaging.text`, which must be parsed.
- The flyer window comes only as badge text.
- `hasVat:false` is a display flag. Consumer prices include VAT (inferred).

### EAN
**Not exposed anywhere.** There is no EAN field in the gridbox JSON. The product-page JSON-LD has `sku` = erpNumber and no `gtin`. Searching by EAN returns `type:"empty"`. `ians` is an internal article number.

**Verdict: possible but painful.** A clean batch API works with no protection. However, there is no EAN, coverage of the everyday range is partial, and the promo window, unit and pack size come as text to parse. It is worth a small connector for the items it does list, with each product matched by hand once.

---

## 4. Penny: flyer only (belief confirmed), and only a small web subset

`www.penny.cz` is a Nuxt app over REWE's `product-discovery` API (commercetools). There was no bot wall and no cookie. Login exists but is not needed and uses reCAPTCHA; I did not touch it.

**Everyday prices:** **no.** The whole product catalogue is the current promo set. `GET /api/product-discovery/products?pageSize=1` → `total: 38`, and every item falls under the categories "VŠECHNY AKCE › Web od 23.09.2026" or "produkty s PENNY kartou", all valid 2026-09-23 to 09-29. That is much smaller than the printed flyer. Searches: "magnesia", "pampers" and "madeta" gave 0 results, "máslo" gave only Perla margarine, and "toffifee" gave 1 result. I did not see a store or region parameter; it seems national **(unverified)**.

### Search
`GET https://www.penny.cz/api/product-discovery/products/search/<urlencoded q>?pageSize=20`

```json
{"total":1,"results":[{"sku":"88-385207","slug":"toffifee-88385207","name":"Toffifee","amount":"125",
 "volumeLabelShort":"g","inPromotion":false,
 "price":{"regular":{"value":2990,"perStandardizedQuantity":2392,"tags":["pt-aktion"]},
          "standard":{"value":4790},"crossed":4790,"lowestPrice":…,
          "validityStart":"2026-09-23","validityEnd":"2026-09-29","baseUnitShort":"g","basePriceFactor":"100"}}]}
```

Prices are in **haléře**: 2990 = 29.90 Kč. `regular.value` is the promo price. `standard.value` or `crossed` is the normal shelf price. PENNY karta items add `price.loyalty:{value:2490,tags:["SO"]}`. `inPromotion` was false even for flyer items, so ignore it.

### Price by id
- Single: `GET https://www.penny.cz/api/product-discovery/products/88-385207` (by sku, not by slug, which returns 404).
- Batch (verified): `GET https://www.penny.cz/api/product-discovery/products?skus=88-385207,88-302594` → `{"total":2,"results":[…]}`.
- Other listings: `/api/product-discovery/categories/tree` and `/api/product-discovery/categories/vsechny-akce-99000000/products?pageSize=500` (the full current web set).

### EAN
Not exposed. No ean or gtin field appears in the search or product JSON, and searching by EAN gives 0 results.

**Verdict: no, flyer only, so rely on Kupi.** Penny's own JSON is clean, but it covers only about 38 web offers per week, far fewer than the flyer. Kupi covers more.

---

## Summary

| store | everyday prices online | EAN | login needed | bot protection | verdict |
|---|---|---|---|---|---|
| Košík.cz | **Yes**, full assortment (regular + promo + unit price) | Search by EAN works (3/4 hit); not a field in the JSON | No | None seen | **Direct connector worth it** (`suggest/v2` → `products/sapid` batch) |
| Kaufland | **No**, flyer only (~811 offers per store per week; `formattedOldPrice` gives the regular price of promo items) | No; only a heuristic from the image file name | No | `www.kaufland.cz` (Marketplace) returns a Cloudflare 403; `prodejny.kaufland.cz` is open | **No, rely on Kupi**. Its own store-specific flyer JSON (with Card price) is available as an optional upgrade |
| Lidl | **Partial**: about 500 food items incl. undated in-store shelf prices; incomplete coverage | No (not in JSON, not in JSON-LD, EAN search empty) | No | None (406 if `Accept: application/json` is sent) | **Possible but painful**: batch `gridboxes` API, but no EAN, partial range, text-only units |
| Penny | **No**: only 38 web promo items | No | No | None seen | **No, rely on Kupi**. Own JSON exists but is a small subset of the flyer |

Scratch files: `survey/` (raw responses `k_*.json`, `kf_*.html`, `lidl_*.json`, `p_*.json`, and the parsers `kf_parse.py` and `lidl_parse.py`).

---

# Survey C: dm, Rossmann, Teta, Makro (price-tracker feasibility)

Tested 2026-09-23 with plain `curl` and a desktop Chrome User-Agent. No cookies, no login. Everything below marked **verified** came back from a live request. Anything marked *inferred* comes from reading JS or guessing at field meanings.

---

## 1. dm (dm.cz)

**Online shop:** yes. It has the full assortment with everyday shelf prices and ships in CZ.

### Search (name or EAN), verified
```
GET https://product-search.services.dmtech.com/cz/search/crawl?query=<text or EAN>&searchType=product&pageSize=<n>
Headers: User-Agent only. No cookies.
```
- `/cz/search?...` (without `/crawl`) returns a **302 to `/crawl`**. Calling `/crawl` directly works.
- An EAN as `query` returns the exact product. Verified for 8006540858073, 8720181481529 and 8718951801400.
- Sanytol 5 L (8411135006829) returned `count:0`. dm doesn't stock it.
```json
{"products":[{"gtin":8006540858073,"dan":2028934,"brandName":"Pampers Premium Care",
  "title":"pleny Premium Care, velikost 1, 72 ks",
  "tileData":{"price":{"prefix":"Cena za ks","price":{"current":{"value":"299,00 Kč"}},
               "tileInfos":["72 ks (4,15 Kč za 1 ks)"]},
              "trackingData":{"currency":"CZK","price":299.0},
              "self":"/p/d/2028934/pampers-premium-care-pleny-premium-care-velikost-1"}}],
 "count":7,"currentPage":0,"pageSize":5,"totalPages":2}
```
- **Rate limiting:** after about 5 quick calls (1 s apart) I got `{"message":"Too many requests! If you need further information please contact searchapi-support@dm.de"}`. It cleared after about 20 s. For a household list, space calls several seconds apart.

### Price by id (batched), verified
```
GET https://products.dm.de/product/products/tiles/CZ/dans/2028934,3161014,2557948
```
- No headers or cookies needed. The response is keyed by DAN.
```json
{"products":{"3161014":{"dan":3161014,"gtin":8718951801400,
   "title":{"tileHeadline":"tekuté mýdlo Ultra, náhradní náplň, 900 ml"},
   "price":{"prefix":"Cena za ks","price":{"current":{"value":"159,00 Kč"}},"tileInfos":["900 ml (17,67 Kč za 100 ml)"]},
   "trackingData":{"price":159.0,"currency":"CZK"}}}}
```
Detail for one product (gzip, so use `--compressed`), verified:
```
GET https://products.dm.de/product/products/detail/CZ/dan/2028934
→ gtin, metadata.price=299.0,
  price.price.current.value="299,00 Kč",
  price.infos=["72 ks (4,15 Kč za 1 ks)","vč. DPH plus poštovné"],
  price.notIncreasedSince.text="nebyla zvýšena od 01.11.2022",
  seoInformation.structuredData.{gtin,price,priceCurrency}
```
- The host config was read from `https://www.dm.cz/scripts/main.js`: `productsGatewayUrl`, `productDataServiceUrl` and `movaUrl` (availability).
- **VAT:** prices include VAT ("vč. DPH").
- **Unit price:** in `tileInfos` / `infos`, as text only.
- **Promo** *(inferred from JS)*: a promo shows as `price.price.previous` (the strike-through price) alongside `current`. None of the tested items was on promo.
- dm has one national price, no loyalty price and no login-only price.
- **Prices tested:**

  | Item | DAN | Price |
  |---|---|---|
  | Pampers PC 1, 72 ks | 2028934 | 299 Kč |
  | Dove Original 4×90 g (EAN 8720181481529, listed as "360 g") | 2557948 | 99 Kč |
  | Protex Ultra refill 900 ml | 3161014 | 159 Kč |

### EAN
Yes. `gtin` is in search, tiles and detail, and search accepts an EAN as the query.

### Login / bot protection
- No login needed.
- No challenge. The HTML pages have reCAPTCHA Enterprise loaded, but the APIs are not gated.
- The only limit is the search rate limit above.

### Verdict: **direct connector worth it**
Clean JSON, EAN search, batched price by DAN, and no auth.

---

## 2. Rossmann (rossmann.cz)

**Online shop:** yes. It has the full assortment. Server-rendered HTML (JSESSIONID, Azure Front Door). Search runs on **Luigi's Box**, which exposes all price tiers.

### Search (name, EAN or Rossmann order number), verified
```
GET https://live.luigisbox.com/search?tracker_id=715445-966389&q=<text|EAN|id_erp>&size=<n>
```
- The tracker id comes from `https://scripts.luigisbox.tech/LBX-966389.js`, which is referenced in the rossmann.cz HTML. No headers or cookies needed.
```json
{"results":{"total_hits":1,"hits":[{"type":"PRODUCT","exact":true,
 "url":"6437009db37471f38c9c0433",
 "attributes":{"title":"Pleny Premium Care, 2–5 kg, vel. 1 72 ks","PRODUCER":["Pampers"],
  "id_erp":["1177954"],
  "id_ean":["08006540858066","08006540858073","8006540858066","8006540858073"],
  "price_regular_CZK_incVat":379.0,
  "price_clubMember_CZK_incVat":379.0,
  "price_rossmanekMember_CZK_incVat":289.0,
  "price_regular_CZK_incVat-zeroVat":...,   // same prices without VAT
  "special_offer":["LONG_TERM_LOWERED_PRICE","ROSSMANEK_PRICE"],"badge":["PROMOTION"],
  "availability":1}}]}}
```
- **EAN:** a query by EAN finds the product (verified for Pampers and Dove).
- **Caution:** when the EAN isn't stocked, Luigi returns *fuzzy junk* with `total_hits:100` (I got a comb and socks for Protex and Sanytol EANs). Always check that the EAN appears in `id_ean`.
- Protex Ultra and Sanytol are stocked, just in different SKUs:
  - "protex ultra": id_erp 1107791, Protex Ultra refill, reg 94.90
  - "sanytol 5": only 500 ml items, no 5 L
- Adding `f[]=type:item` gives 0 hits. Filtering with `f[]=id_erp:` or `f[]=id_ean:` together with `q=*` also returned 0.

### Price by id, verified
- There's no batch endpoint. Instead, `q=<id_erp>` returns exactly that product (verified: `q=1177954` gives one exact hit). That means one call per item.
- Fallback: the product HTML page lists `EAN` and `Obj. č.` (the order number), and the category grid shows `product-tile__price--final`. The product page has no Product JSON-LD.

### Price fields
- `price_regular_*` is the everyday shelf price.
- `price_clubMember_*` is the Rossmann Club (app) price.
- `price_rossmanekMember_*` is the Rossmánek (kids club) price.
- `*_incVat` includes VAT. `*_incVat-zeroVat` is the price without VAT.
- `special_offer` / `badge` flag promos.
- Pack size is only in the title. There's no separate unit-price field.

**Prices tested:**

| Item | id_erp | Regular | Club | Rossmánek |
|---|---|---|---|---|
| Pampers PC 1, 72 ks | 1177954 | 379 | 379 | 289 |
| Dove multipack 4×90 g | 1125337 | 139 | 111 | 111 |

### Login / bot protection
No login needed and no bot protection seen. Plain curl works on both rossmann.cz and Luigi's Box.

### Verdict: **direct connector worth it**
One public JSON search call gives EAN, regular price and loyalty prices. Just verify the EAN match.

---

## 3. Teta drogerie (tetadrogerie.cz)

**Online shop:** yes. Nuxt frontend plus a Sylius API backend (`apiBase:"https://be.tetadrogerie.cz"` in the page config). It has the full assortment: the public variant collection reports **36,932 variants**.

### Search (name or EAN), verified
```
GET https://live.luigisbox.com/search?tracker_id=407635-780988&q=<text|EAN>&size=<n>&f[]=type:product_variant
```
The tracker id is `lbTrackerId` in the tetadrogerie.cz HTML config.
```json
{"type":"product_variant","url":"product_variant-61182","exact":true,
 "attributes":{"title":"Pampers Premium Care, vel. 1, 72 ks","code":["264929"],"ean":["8006540858073"],
  "brand":["Pampers"],"price":"35900","original_price":"64990",
  "prices":"{\"zcmd\":39900,\"acmd\":35900,\"unit_price\":{\"number\":498.61,\"unit\":\"KS\"},\"sale\":{\"amount\":8000},\"conditions\":\"za 1 ks při koupi 2 ks ...\"}",
  "in_stock":"True","stock_on_stores":[499],
  "web_url":["/api/v2/shop/product-variants/000000000000264929"],"slug":["pampers-premium-vp-s1-(72ks-fol)"]}}
```
- **All amounts are in haléře (×100).**
- **EAN:** a query by EAN finds the product. Verified for 8006540858073 and for 8720181481529 (Dove 4×90 g, zcmd 149.90).
- Unknown EANs return fuzzy hits, so check `ean`. This happened for Protex 8718951801400 and Sanytol 8411135006829.
- Name searches that worked:
  - "protex ultra": code 238529, Protex Ultra refill 700 ml, zcmd 159.90
  - No Sanytol 5 L; only Sanytol 500 ml (code 269648)

### Price by id, verified
```
GET https://be.tetadrogerie.cz/api/v2/shop/product-variants/000000000000264929
Accept: application/ld+json
```
The id is the variant code, zero-padded to 18 digits.
```json
{"code":"000000000000264929","name":"Pampers Premium Care, vel. 1, 72 ks","currency":"CZK",
 "bbyPrices":{"zcmd":39900,"acmd":35900,"unit_price":{"name":"standard","number":498.61,"unit":"KS"},
              "sale":{"unit":"Kč","amount":8000},"conditions":"za 1 ks při koupi 2 ks ..."},
 "sapStatus":1,"enabled":true}
```
Elmex example: `{"zcmd":12490,"club":10990,"acmd":12490,"unit_price":{"number":146533.33,"unit":"LIT"}}`

**Batch:**
- `GET /api/v2/shop/product-variants?itemsPerPage=N&page=P` lists the **whole catalog** with `bbyPrices` (18,466 pages at 2 per page).
- `code[]` filters are ignored. The only supported filters are `product` and `optionValues` (from `hydra:search`).
- So either make one GET per code, or dump the full catalog occasionally.
- The variant JSON has **no EAN**. Take the EAN from Luigi's Box.

### Price fields *(inferred from values)*
- `zcmd` is the "základní cena", the everyday shelf price.
- `acmd` is the "akční cena", a promo that is often conditional (see `conditions`).
- `club` is the Teta club price.
- `unit_price.number` is haléře per KS / LIT / KG. It seems to be computed from the lowest price.
- Luigi's `original_price` is sometimes higher than `zcmd`, probably an RRP. Don't use it.
- VAT is included (consumer shop). *Inferred.*

### Login / bot protection
No login needed and no bot protection seen. Both www (cached) and `be.` answered plain curl.

### Verdict: **direct connector worth it**
Public Sylius JSON with an explicit shelf price vs promo vs club price, plus EAN lookup via Luigi's Box.

---

## 4. Makro (sortiment.makro.cz)

**Online shop:**
- `https://www.makro.cz/shop` returned **HTTP 403 from Akamai** to plain curl. I stopped there, as the rules require.
- `https://sortiment.makro.cz/shop` is the METRO "sortiment" app. It answered without any challenge.
- It has the full assortment, with separate prices per store. Store ids come from `GET https://sortiment.makro.cz/cia/content/sitecore/storeIdMappingWithOnlineVisibility/CZ/cs-CZ`: 00002–00015. I used 00011.
- *Not verified:* which store id is the Prague one.

### Search (name or EAN), verified, returns a price without login
```
GET https://sortiment.makro.cz/searchdiscover/articlesearch/search?storeId=00011&language=cs-CZ&country=CZ&query=<text|EAN|BTY-id>&rows=<n>&page=1
Accept: application/json
```
```json
{"amount":1,"query":"8593803421216","resultIds":["BTY-X1154730032"],
 "results":{"BTY-X1154730032":{"id":"BTY-X1154730032","price":52.3,"isAvailable":true,"offers":[]}}}
```
- Adding `filter=delivery_mode:METRO_SHOP` gave HTTP 500.
- EAN search worked for every test item:

  | EAN | Makro id | `price` |
  |---|---|---|
  | 8593803421216 (butter) | BTY-X1154730032 | 52.3 |
  | 8006540858073 (Pampers) | BTY-X4736490032 | 369.0 |
  | 8720181481529 (Dove) | BTY-X4861650032 | 21.5 |
  | 8411135006829 (Sanytol 5 L) | BTY-X4661790032 | 559.0 (isAvailable false) |

- A query with the BTY id returns that item, so the same endpoint also works as "price by id", one call per item.
- **What `price` means is unverified.** It is likely the net price (bez DPH) per selling unit.
- Dove "4 x 90 g" at 21.5 looks like a **per-piece** price, not per pack. Check this against the UI (or with a session) before trusting unit semantics.

### Article detail (batched), verified, but no price without login
```
GET https://sortiment.makro.cz/evaluate.article.v1/betty-variants?storeIds=00011&country=CZ&locale=cs-CZ&ids=BTY-X4861650032&ids=BTY-X4661790032&details=true
```
The response gives, for each bundle:
- `description`
- `gtins[].number` (EAN, verified)
- `contentData.netPieceWeight`
- `basePriceContent` / `basePriceContentMeasureUnit`
- `stores.00011.sellingPriceInfo`, which is **null** when anonymous
- `anonymousFlags.buyable:false`

Two things to know:
- An article has several bundles. For example, butter 0021 is the 40×250 g carton (EAN 8593803421261) and 0022 is a single 250 g (EAN 8593803421216).
- *Inferred:* with the user's session cookie, `sellingPriceInfo` probably carries the net and gross price and promos. I did not verify this.
- `betty-articles?ids=` returned NOT_FOUND for variant ids.

### Login / bot protection
- **www.makro.cz** is behind Akamai and returned 403 to curl.
- **sortiment.makro.cz** has no challenge (served via Google).
- `robots.txt` disallows `/shop/search?*`, but not the API paths.
- The search price is visible anonymously. Detailed pricing (net vs gross, promo) needs login.

### Verdict: **possible but painful**
- EAN search and an anonymous price work.
- But the VAT basis and unit (piece vs pack) of `price` are unverified.
- Full price detail needs the user's session cookie.
- The main www shop is Akamai-blocked.

---

## Summary

| Store | Everyday prices online | EAN | Login needed | Bot protection | Verdict |
|---|---|---|---|---|---|
| dm | Yes. Single national price, incl. VAT (tiles API, batched by DAN) | Yes: `gtin` field, and search by EAN | No | None on APIs. Search API rate-limits after ~5 quick calls ("Too many requests") | Direct connector worth it |
| Rossmann | Yes, via Luigi's Box: regular, Club and Rossmánek prices, with and without VAT | Yes: `id_ean`, search by EAN (fuzzy fallback, so verify) | No | None seen | Direct connector worth it |
| Teta | Yes: Sylius `bbyPrices` (zcmd shelf / acmd promo / club, in haléře) | Via Luigi's Box `ean` (not in Sylius JSON) | No | None seen | Direct connector worth it |
| Makro | Partly: search returns `price` anonymously (probably net, unit unclear); detailed `sellingPriceInfo` needs login | Yes: search by EAN, `gtins` in betty-variants | For full or confirmed prices, yes (user's cookie) | www.makro.cz: Akamai 403. sortiment.makro.cz: none | Possible but painful |
