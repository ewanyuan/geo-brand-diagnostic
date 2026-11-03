"""Transparent calculations for the local GEO diagnostic product."""

from collections import Counter, defaultdict


def _rate(numerator, denominator):
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _rank(value):
    try:
        rank = int(value)
        return rank if rank > 0 else None
    except (TypeError, ValueError):
        return None


def build_analytics(project, questions, observations, *, start=None, end=None,
                    platform=None, question_id=None):
    """Calculate rates from completed answers, preserving their denominators.

    Offline/error rows remain visible in the detail table but never enter rate
    denominators. A name in free text establishes a mention, not a rank.
    """
    qmap = {q["id"]: q for q in questions}
    selected = []
    for row in observations:
        if start and row["observed_at"] < start:
            continue
        if end and row["observed_at"] > end:
            continue
        if platform and platform != "all" and row["platform"] != platform:
            continue
        if question_id and str(row["question_id"]) != str(question_id):
            continue
        selected.append({**row, "question": qmap.get(row["question_id"], {}).get("text", "已删除问题"),
                         "question_type": qmap.get(row["question_id"], {}).get("kind", "keyword")})

    complete = [r for r in selected if r["status"] == "complete"]
    denominator = len(complete)
    mentioned = [r for r in complete if r["brand_mentioned"] or _rank(r["brand_rank"]) is not None]
    top3 = [r for r in complete if (_rank(r["brand_rank"]) or 999) <= 3]
    first = [r for r in complete if _rank(r["brand_rank"]) == 1]
    classified = [r for r in mentioned if r["sentiment"] in ("positive", "neutral", "negative")]
    positive = sum(r["sentiment"] == "positive" for r in classified)
    negative = sum(r["sentiment"] == "negative" for r in classified)
    def rank_checked(row):
        return bool(row.get("rank_reviewed") or _rank(row["brand_rank"]) is not None)
    rank_sample = sum(rank_checked(r) for r in complete)

    metrics = {
        "answers": denominator,
        "offline": len(selected) - denominator,
        "mentioned": len(mentioned),
        "top3_count": len(top3),
        "first_count": len(first),
        "rank_sample": rank_sample,
        "top3_rate": _rate(len(top3), denominator) if rank_sample == denominator else None,
        "mention_rate": _rate(len(mentioned), denominator),
        "first_rate": _rate(len(first), denominator) if rank_sample == denominator else None,
        "sentiment_sample": len(classified),
        "positive_count": positive,
        "negative_count": negative,
        "positive_rate": _rate(positive, len(classified)),
        "negative_rate": _rate(negative, len(classified)),
    }

    def group_rates(rows):
        good = [r for r in rows if r["status"] == "complete"]
        total = len(good)
        return {
            "answers": total,
            "mentioned": sum(bool(r["brand_mentioned"] or _rank(r["brand_rank"])) for r in good),
            "top3_rate": (_rate(sum((_rank(r["brand_rank"]) or 999) <= 3 for r in good), total)
                          if all(rank_checked(r) for r in good) else None),
            "mention_rate": _rate(sum(bool(r["brand_mentioned"] or _rank(r["brand_rank"])) for r in good), total),
            "first_rate": (_rate(sum(_rank(r["brand_rank"]) == 1 for r in good), total)
                           if all(rank_checked(r) for r in good) else None),
        }

    day_groups = defaultdict(list)
    platform_groups = defaultdict(list)
    question_groups = defaultdict(list)
    for row in selected:
        day_groups[row["observed_at"]].append(row)
        platform_groups[row["platform"]].append(row)
        question_groups[row["question_id"]].append(row)

    trend = [{"date": day, **group_rates(rows)} for day, rows in sorted(day_groups.items())]
    by_platform = [{"platform": name, **group_rates(rows)}
                   for name, rows in sorted(platform_groups.items())]
    by_question = [{"question_id": qid, "question": qmap.get(qid, {}).get("text", "已删除问题"),
                    "kind": qmap.get(qid, {}).get("kind", "keyword"), **group_rates(rows)}
                   for qid, rows in question_groups.items()]
    by_question.sort(key=lambda r: (-r["answers"], r["question"]))

    rivals = []
    for name in project["competitors"]:
        rival_rows = []
        for row in complete:
            rank = _rank(row["competitor_ranks"].get(name))
            if name in row["competitor_ranks"] or (name and name.casefold() in row["answer"].casefold()):
                rival_rows.append((row, rank))
        ranks_known = all(rank is not None or name in row["competitor_ranks"] for row, rank in rival_rows)
        rivals.append({
            "name": name,
            "mentioned": len(rival_rows),
            "mention_rate": _rate(len(rival_rows), denominator),
            "top3_rate": _rate(sum(rank is not None and rank <= 3 for _, rank in rival_rows), denominator) if ranks_known else None,
            "first_rate": _rate(sum(rank == 1 for _, rank in rival_rows), denominator) if ranks_known else None,
        })
    rivals.sort(key=lambda r: (-(r["top3_rate"] or 0), -r["mention_rate"], r["name"]))
    metrics["top3_benchmark"] = rivals[2]["top3_rate"] if len(rivals) >= 3 else (rivals[0]["top3_rate"] if rivals else 0.0)
    if any(r["top3_rate"] is None for r in rivals):
        metrics["top3_benchmark"] = None

    source_counts = Counter()
    article_map = {}
    total_citations = 0
    for row in complete:
        for citation in row["citations"]:
            source = (citation.get("source") or "未注明来源").strip()
            title = (citation.get("title") or citation.get("url") or "未命名文章").strip()
            url = (citation.get("url") or "").strip()
            source_counts[source] += 1
            total_citations += 1
            key = url or f"{source}|{title}"
            if key not in article_map:
                article_map[key] = {"title": title, "source": source, "url": url, "count": 0}
            article_map[key]["count"] += 1
    sources = [{"source": name, "count": count, "share": _rate(count, total_citations)}
               for name, count in source_counts.most_common()]
    articles = sorted(article_map.values(), key=lambda r: (-r["count"], r["title"]))
    for article in articles:
        article["share"] = _rate(article["count"], total_citations)

    positive_terms = Counter(term for row in complete for term in row["positive_terms"])
    negative_terms = Counter(term for row in complete for term in row["negative_terms"])
    return {
        "project": project,
        "metrics": metrics,
        "trend": trend,
        "by_platform": by_platform,
        "by_question": by_question,
        "competitors": rivals,
        "sources": sources,
        "articles": articles,
        "positive_terms": [{"term": term, "count": count} for term, count in positive_terms.most_common(30)],
        "negative_terms": [{"term": term, "count": count} for term, count in negative_terms.most_common(30)],
        "observations": sorted(selected, key=lambda r: (r["observed_at"], r["id"]), reverse=True),
        "filters": {"start": start, "end": end, "platform": platform or "all", "question_id": question_id},
    }
