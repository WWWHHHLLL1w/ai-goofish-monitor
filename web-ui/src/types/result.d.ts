export interface ProductInfo {
  "商品标题": string
  "当前售价": string
  "商品原价"?: string
  "“想要”人数"?: string | number
  "商品标签"?: string[]
  "发货地区"?: string
  "卖家昵称"?: string
  "商品链接": string
  "发布时间"?: string
  "商品ID": string
  "商品图片列表"?: string[]
  "商品主图链接"?: string
  "浏览量"?: string | number
}

export interface SellerInfo {
  "卖家昵称"?: string
  "卖家头像链接"?: string
  "卖家个性签名"?: string
  "卖家在售/已售商品数"?: string
  "卖家收到的评价总数"?: string
  "卖家信用等级"?: string
  "买家信用等级"?: string
}

export interface PriceInsight {
  observation_count: number
  current_price?: number | null
  avg_price?: number | null
  median_price?: number | null
  min_price?: number | null
  max_price?: number | null
  market_avg_price?: number | null
  market_median_price?: number | null
  price_change_amount?: number | null
  price_change_percent?: number | null
  deal_score?: number | null
  deal_label?: string
  first_seen_at?: string | null
  last_seen_at?: string | null
}

export interface ResultInsights {
  market_summary: {
    sample_count: number
    avg_price: number | null
    median_price: number | null
    min_price: number | null
    max_price: number | null
    snapshot_time?: string | null
  }
  history_summary: {
    unique_items: number
    sample_count: number
    avg_price: number | null
    median_price: number | null
    min_price: number | null
    max_price: number | null
  }
  daily_trend: Array<{
    day: string
    sample_count: number
    avg_price: number | null
    median_price: number | null
    min_price: number | null
    max_price: number | null
  }>
  latest_snapshot_at?: string | null
}

export interface ResultItem {
  "爬取时间": string
  "搜索关键字": string
  "任务名称": string
  "商品信息": ProductInfo
  "卖家信息"?: SellerInfo
  ai_analysis?: Record<string, unknown>
  price_insight?: PriceInsight
  _status?: 'active' | 'hidden' | 'expired'
  _effective_hidden?: boolean
  _hidden_reason?: 'manual' | 'rule' | 'expired' | null
  _matched_blacklist_keywords?: string[]
}
