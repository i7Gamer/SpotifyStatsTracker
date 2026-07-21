from __future__ import annotations

from flask import request, url_for
from config import DEFAULT_SORT_BY, PAGE_SIZE, VALID_SORT_BY


class PaginationMixin:
    """Page-number link building, pagination context, and page/sort query-param parsing."""

    def _buildPageUrl(self, endpoint, page, **queryArgs):
        cleanArgs = {key: value for key, value in queryArgs.items() if value not in (None, "")}
        cleanArgs["page"] = page
        return url_for(endpoint, **cleanArgs)

    def _getNeighboringUrls(self, name, page, totalPages, **queryArgs):
        prevUrl = self._buildPageUrl(name, page - 1, **queryArgs) if page > 1 else None
        nextUrl = self._buildPageUrl(name, page + 1, **queryArgs) if page < totalPages else None
        return prevUrl, nextUrl

    def _buildPageNumberLinks(self, endpoint, page, totalPages, window=2, **queryArgs):
        """Page-number links for a pagination strip: always page 1 and the last
        page, plus a `window`-page radius around the current page, with an
        {"ellipsis": True} marker filling any gap between shown pages."""
        if totalPages <= 1:
            return []

        pagesToShow = {1, totalPages}
        for p in range(page - window, page + window + 1):
            if 1 <= p <= totalPages:
                pagesToShow.add(p)

        links = []
        previousPage = None
        for p in sorted(pagesToShow):
            if previousPage is not None and p - previousPage > 1:
                links.append({"ellipsis": True})
            links.append({"num": p, "url": self._buildPageUrl(endpoint, p, **queryArgs), "current": p == page})
            previousPage = p
        return links

    def _buildPaginationContext(self, endpoint, page, totalPages, totalCount, pageSize=PAGE_SIZE, **queryArgs):
        """Everything a list page's pagination strip needs: prev/next links,
        windowed page-number links, and the 'Showing X-Y of Z' counts."""
        prevUrl, nextUrl = self._getNeighboringUrls(endpoint, page, totalPages, **queryArgs)
        pageLinks = self._buildPageNumberLinks(endpoint, page, totalPages, **queryArgs)
        showingStart = (page - 1) * pageSize + 1 if totalCount else 0
        showingEnd = min(page * pageSize, totalCount)
        return {
            "page": page,
            "totalPages": totalPages,
            "prevUrl": prevUrl,
            "nextUrl": nextUrl,
            "pageLinks": pageLinks,
            "showingStart": showingStart,
            "showingEnd": showingEnd,
            "totalCount": totalCount,
        }

    def _getPageParam(self):
        """The current request's ?page=... as an int >= 1, tolerating junk input."""
        try:
            return max(1, int(request.args.get("page", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _getSortByParam(self, default=DEFAULT_SORT_BY):
        """The current request's ?sortBy=..., falling back to `default` for any
        value the DB layer doesn't know how to sort by (see VALID_SORT_BY) -
        without this, an unrecognized value reaches a ValueError/KeyError deep
        in Repository/Database and 500s instead of just using the default."""
        sortBy = request.args.get("sortBy", default)
        return sortBy if sortBy in VALID_SORT_BY else default

    def _calculatePagination(self, totalCount):
        """Calculate safe page bounds given a total count.
        Returns (page, totalPages, startIndex) where page is clamped to valid range."""
        page = self._getPageParam()
        totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, totalPages))
        startIndex = (page - 1) * PAGE_SIZE
        return page, totalPages, startIndex
