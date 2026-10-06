# Changelog

## 0.1.0 (2026-10-06)


### Features

* **balances:** per-friend and per-group balance summaries ([9c18fe5](https://github.com/trustxai/splitwise-mcp/commit/9c18fe55a23e40948a3815ce529056b0c4c97347))
* **client:** SplitwiseEnvelopeError carries the body; partial batch outcomes rendered; path-aware 400 hint ([3d01617](https://github.com/trustxai/splitwise-mcp/commit/3d016177b01f8fdec7b54b4c7fe92b861b0c1d3b))
* **comments,notifications:** expense comments and the activity feed ([6afda31](https://github.com/trustxai/splitwise-mcp/commit/6afda31d954d62c4c5cd15ecbca198c7d8bdac58))
* **config:** SPLITWISE_MCP_ALLOWED_ORIGINS for the http transport's Origin check ([0e190ed](https://github.com/trustxai/splitwise-mcp/commit/0e190ed32bd494a14ca492521b00f70f15ef7c7f))
* **expenses:** list/get/create/update/delete/undelete expenses with local share validation ([12a3b73](https://github.com/trustxai/splitwise-mcp/commit/12a3b736269247397215a7fa2d65e0ff6914d56a))
* **friends:** list/get friends with balances, add/add-many/delete friends ([0f889d8](https://github.com/trustxai/splitwise-mcp/commit/0f889d88291804879907bc49c303cf32b13ba70d))
* **groups:** list/get/create/delete/undelete groups and membership changes ([4f5f5cd](https://github.com/trustxai/splitwise-mcp/commit/4f5f5cdbc8149a3d4d508c5b42e6076f81bc6a5c))
* **http:** streamable-http transport behind a constant-time bearer middleware ([d7c0ce8](https://github.com/trustxai/splitwise-mcp/commit/d7c0ce8bceaa443ca5cba9e386b237c78903bb5d))
* **lookup:** categories, currencies and fuzzy name resolution ([107f6c2](https://github.com/trustxai/splitwise-mcp/commit/107f6c23711d23b96ad6aa4a74899ba1508dd3b0))
* Stage 0 foundation (spine, Bearer client with write kill-switch, stub registry, oracle, CI, release tooling) ([ab646db](https://github.com/trustxai/splitwise-mcp/commit/ab646dbfedaad7e9f05606b4e0bea52a51b669c9))
* **users:** current user, get user, update profile (name/locale/currency) ([795c34c](https://github.com/trustxai/splitwise-mcp/commit/795c34c49793b8d260ec62607f13723e1e3cab59))


### Bug Fixes

* **balances:** apply review findings ([cbce74b](https://github.com/trustxai/splitwise-mcp/commit/cbce74b0e2fca1f059f0d6260ffa1533ea3b846b))
* **client:** apply review findings on the partial-outcome rendering ([2691cda](https://github.com/trustxai/splitwise-mcp/commit/2691cdabb7445ddd61cd8ac9af1700c028a4e4c7))
* **comments,notifications:** apply review findings ([45f73b4](https://github.com/trustxai/splitwise-mcp/commit/45f73b4ac484b3eb7ece86c4a7f69a5585ca059e))
* **expenses:** apply review findings ([1a11658](https://github.com/trustxai/splitwise-mcp/commit/1a116580873bdd68f3f88cefd674d38be6b7805e))
* **friends:** apply review findings ([a4c0b29](https://github.com/trustxai/splitwise-mcp/commit/a4c0b2946314d6e2f60630a3f9a19eb107b1ae30))
* **groups:** apply review findings ([2b0acb7](https://github.com/trustxai/splitwise-mcp/commit/2b0acb79045b175e3d943730f3c9ac7f739b6924))
* **http:** apply security review findings ([d89afaa](https://github.com/trustxai/splitwise-mcp/commit/d89afaac96ddbecf6b323f674dec289c2dc6ada6))
* **lookup:** apply review findings ([405bd6a](https://github.com/trustxai/splitwise-mcp/commit/405bd6af2b301a4f752b652073dc608a7f579a87))
* **lookup:** live smoke — category names are localized to the account's locale ([daefbc9](https://github.com/trustxai/splitwise-mcp/commit/daefbc9232b4ea55404928440a393373e7633de6))
* **users:** apply review findings ([e7b13b0](https://github.com/trustxai/splitwise-mcp/commit/e7b13b09f4f2f36d48078cb038261676164e89f4))
* **validators:** reject absurd money magnitudes with a readable error ([3a9d839](https://github.com/trustxai/splitwise-mcp/commit/3a9d839f955ed49182eb74ad66c1e7e27ced833e))


### Documentation

* README — safety model, features, client configs, the remote transport and troubleshooting ([440588d](https://github.com/trustxai/splitwise-mcp/commit/440588dc6d203aa39c77c0cdd51a26c16a389768))
* regenerate the tool table — 33 tools ([54ce274](https://github.com/trustxai/splitwise-mcp/commit/54ce2748529dd082a31f51e84894ba58afcbb61a))
* the API key requires Splitwise Pro ([97e74dd](https://github.com/trustxai/splitwise-mcp/commit/97e74ddbd2212c9df21c1f5719633569a2c9753e))

## Changelog
