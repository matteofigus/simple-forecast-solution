import os
import time
import datetime
import uuid
import base64
import pathlib
import textwrap

import numpy as np
import pandas as pd
import awswrangler as wr
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

from collections import OrderedDict
from concurrent import futures
from tabulate import tabulate
from stqdm import stqdm
from SessionState import get_state
from sfs import (load_data, resample, run_pipeline, run_cv_select,
    make_demand_classification, make_perf_summary,
    make_health_summary, GROUP_COLS, EXP_COLS)

from lambdamap import LambdaExecutor, LambdaFunction

ST_STATIC_PATH = pathlib.Path(st.__path__[0]).joinpath("static")
ST_DOWNLOADS_PATH = ST_STATIC_PATH.joinpath("downloads")

if not os.path.exists(ST_DOWNLOADS_PATH):
    ST_DOWNLOADS_PATH.mkdir()

FREQ_MAP = OrderedDict(Daily="D", Weekly="W-MON", Monthly="MS")


def validate(df):
    """Validate a dataset.
    """

    err_msgs = []
    warn_msgs = []

    # check column names
    for col in EXP_COLS:
        if col not in df:
            err_msgs.append(f"missing **{col}** column")

    msgs = {
        "errors": err_msgs,
        "warnings": warn_msgs
    }

    is_valid_file = len(err_msgs) == 0

    return msgs, is_valid_file


@st.cache
def load_uploaded_file(uploaded_file):
    """
    """

    if uploaded_file.name.endswith(".csv.gz"):
        df = pd.read_csv(uploaded_file, compression="gzip")
    elif uploaded_file.name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    else:
        raise NotImplementedError

    # reset read position to start of file
    uploaded_file.seek(0, 0)

    return df


class StreamlitExecutor(LambdaExecutor):
    """Custom LambdaExecutor to display a progress bar in the app.
    """

    def map(self, func, payloads, local_mode=False):
        """
        """

        if local_mode:
            f = func
        else:
            f = LambdaFunction(func, self._client, self._lambda_arn)
        
        ex = self._executor
        wait_for = [ex.submit(f, *p["args"], **p["kwargs"]) for p in payloads]

        return wait_for


def display_progress(wait_for, desc=None):
    """
    """

    # display progress of the futures
    pbar = stqdm(desc=desc, total=len(wait_for))
    prev_n_done = 0
    n_done = sum(f.done() for f in wait_for)

    while n_done != len(wait_for):
        diff = n_done - prev_n_done
        pbar.update(diff)
        prev_n_done = n_done
        n_done = sum(f.done() for f in wait_for)
        time.sleep(0.5)

    diff = n_done - prev_n_done
    pbar.update(diff)

    return


def run_lambdamap(df, horiz, freq):
    """
    """

    payloads = []
    groups = df.groupby(GROUP_COLS, as_index=False, sort=False)

    # generate payload
    for _, dd in groups:
        payloads.append(
            {"args": (dd, horiz, freq),
             "kwargs": {"obj_metric": "smape_mean", "cv_stride": 2}})

    executor = StreamlitExecutor(max_workers=min(1000, len(payloads)),
                                 lambda_arn="LambdaMapFunction")
    wait_for = executor.map(run_cv_select, payloads)

    return wait_for


#
# Panels
#
def panel_launch_forecast(state):
    """
    """

    with st.beta_container():
        st.subheader("Step 2: Launch Forecast")

        with st.form("form_create_forecast"):
            _cols = st.beta_columns(2)

            with _cols[0]:
                horiz = st.number_input("Forecast horizon length", min_value=1)

            with _cols[1]:
                freq_out = st.selectbox("Forecast Frequency", list(FREQ_MAP.keys()))

            create_forecast_button = st.form_submit_button("Launch")


    if create_forecast_button:
        state.horiz = horiz
        state.freq_out = freq_out
        wait_for = \
            run_pipeline(state.df, FREQ_MAP[state.freq_in],
                FREQ_MAP[state.freq_out], obj_metric="smape_mean", cv_stride=4,
                backend="futures", horiz=state.horiz)

        display_progress(wait_for)

        # aggregate the forecasts
        raw_results = [f.result() for f in futures.as_completed(wait_for)]

        pred_lst = []
        results_lst = []

        for df_pred, df_results in raw_results:
            pred_lst.append(df_pred)
            results_lst.append(df_results)

        # results dataframe
        state.df_results = pd.concat(results_lst) \
                             .reset_index(drop=True)

        # predictions dataframe
        state.df_pred = pd.concat(pred_lst)
        state.df_pred.index.name = "timestamp"
        state.df_pred.reset_index(inplace=True)

        # analysis dataframes
        state.df_demand_cln = \
            make_demand_classification(state.df, FREQ_MAP[state.freq_out])
        state.perf_summary = make_perf_summary(state.df_results)

    return


def make_mask(df, channel, family, item_id):
    mask = np.ones(len(df)).astype(bool)

    # only mask when all three keys are non-empty
    if channel == "" or family == "" or item_id == "":
        return ~mask

    mask &= df["channel"] == channel
    mask &= df["family"] == family
    mask &= df["item_id"] == item_id

    return mask


def panel_visualization(state):
    """
    """

    df_pred = state.df_pred
    df_results = state.df_results
    df_top = state.df_top

    channel_vals = [""] + sorted(df_results["channel"].unique())
    family_vals = [""] + sorted(df_results["family"].unique())
    item_id_vals = [""] + sorted(df_results["item_id"].unique())

    channel_index = channel_vals.index(df_top["channel"].iloc[0])
    family_index = family_vals.index(df_top["family"].iloc[0])
    item_id_index = item_id_vals.index(df_top["item_id"].iloc[0])

    _cols = st.beta_columns(3)

    with _cols[0]:
        channel_choice = st.selectbox("Channel", channel_vals, index=channel_index)

    with _cols[1]:
        family_choice = st.selectbox("Family", family_vals, index=family_index)

    with _cols[2]:
        item_id_choice = st.selectbox("Item ID", item_id_vals, index=item_id_index)

    results_mask = \
        make_mask(df_results, channel_choice, family_choice, item_id_choice)
    pred_mask = \
        make_mask(df_pred, channel_choice, family_choice, item_id_choice)

    df_plot = df_pred[pred_mask]

    if len(df_plot) > 0:

        # display the line chart
        #fig = px.line(df_plot, x="timestamp", y="demand", color="type")

        y = df_plot.query("type == 'actual'")["demand"]
        y_ts = df_plot.query("type == 'actual'")["timestamp"]

        yp = df_plot.query("type == 'fcast'")["demand"]
        yp_ts = df_plot.query("type == 'fcast'")["timestamp"]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=y_ts, y=y, mode='lines+markers', name="actual",
            marker=dict(size=4)
        ))
        fig.add_trace(go.Scatter(
            x=yp_ts, y=yp, mode='lines+markers', name="forecast", line_dash="dot",
            marker=dict(size=4)
        ))
#       fig.update_layout(
#           xaxis={
#               "showgrid": True,
#               "gridcolor": "lightgrey",
#           },
#           yaxis={
#               "showgrid": True,
#               "gridcolor": "lightgrey",
#           }
#       )
        fig.update_layout(
            margin={"t": 0, "b": 0, "r": 0, "l": 0},
            height=250,
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.0, "xanchor":"left", "x": 0.0}
        )
        st.plotly_chart(fig, use_container_width=True)

    return


def panel_prepare_data(state):
    """
    """

    st.markdown(textwrap.dedent("""
    #### Input Data Description

    The input data must be in the form of a single CSV (`.csv`) or
    GZipped CSV (`.csv.gz`) file with the following columns:

    `timestamp` – date of the demand in the format `%Y-%m-%d`  
    `channel` – platform/store where the demand/sale originated  
    `family` – item family or category  
    `item_id` – unique identifier/SKU of the item  
    `demand` – demand for the item

    Each row indicates the demand for a particular item for a given date. 
    Each timeseries is identified by its `channel`, `family`, and `item_id`.

    #### Example

    ```
    timestamp,channel,family,item_id,demand
    2018-07-02,Website,Shirts,SKU29292,254
    2018-07-03,Store,Footwear,SKU29293,413
    ...
    ```"""))

    return


def panel_health_check(state):
    """
    """

    df_health = state.df_health

    num_series = df_health.shape[0]
    num_channels = df_health["channel"].nunique()
    num_families = df_health["family"].nunique()
    num_item_ids = df_health["item_id"].nunique()
    first_date = df_health['timestamp_min'].dt.strftime('%Y-%m-%d').min()
    last_date = df_health['timestamp_max'].dt.strftime('%Y-%m-%d').max()

    if state.freq_in == 'Daily':
        duration_unit = 'D'
        duration_str = 'days'
    elif state.freq_in == 'Weekly':
        duration_unit = 'W'
        duration_str = 'weeks'
    elif state.freq_in == 'Monthly':
        duration_unit = 'M'
        duration_str = 'months'
    else:
        raise NotImplementedError

    duration = pd.Timestamp(last_date).to_period(duration_unit) - \
               pd.Timestamp(first_date).to_period(duration_unit)

    pc_missing = \
        df_health["demand_missing_dates"].sum() / df_health["demand_len"].sum()

    with st.beta_container():
        _cols = st.beta_columns(3)

        with _cols[0]:
            st.markdown("#### Summary")
            st.text(f"Frequency:\t{state.freq_in}\n"
                    f"No. series:\t{num_series}\n"
                    f"No. channels:\t{num_channels}\n"
                    f"No. families:\t{num_families}\n"
                    f"No. item IDs:\t{num_item_ids}"
                )

        with _cols[1]:
            st.markdown("#### Timespan")
            st.text(f"Duration:\t{duration.n} {duration_str}\n"
                    f"First date:\t{first_date}\n"
                    f"Last date:\t{last_date}\n"
                    f"% missing:\t{int(np.round(pc_missing*100,0))}")

        with _cols[2]:
            st.markdown("#### Timeseries Lengths")

            fig = px.box(df_health, x="demand_nonnull_count", height=160)
            fig.update_layout(
                margin={"t": 5, "b": 0, "r": 0, "l": 0},
                xaxis_title=duration_str,
                height=100
            )

            st.plotly_chart(fig, use_container_width=True)

    return


def panel_forecast_summary(state):
    """
    """
    df_demand_cln = state.df_demand_cln
    df_results = state.df_results

    df_cln = pd.DataFrame({"category": ["short", "medium", "continuous"]})
    df_cln = df_cln.merge(
        df_demand_cln["category"]
            .value_counts(normalize=True)
            .reset_index()
            .rename({"index": "category", "category": "frac"}, axis=1),
        on="category", how="left"
    )
    df_cln = df_cln.fillna(0.0)
    df_cln["frac"] *= 100
    df_cln["frac"] = df_cln["frac"].astype(int)

    _cols = st.beta_columns(3)

    with _cols[0]:
        st.markdown("#### Parameters")
        st.text(f"Horiz. Length:\t{state.horiz}\n"
                f"Frequency:\t{state.freq_out}")

        st.markdown("#### Classification")
        st.text(f"Short:\t\t{df_cln.iloc[0]['frac']} %\n"
                f"Medium:\t\t{df_cln.iloc[1]['frac']} %\n"
                f"Continuous:\t{df_cln.iloc[2]['frac']} %")

    df_model_dist, sr_err, sr_err_naive, acc_increase = \
        make_perf_summary(df_results)

    with _cols[1]:
        st.markdown("#### Best Models")
        df_model_dist = df_model_dist.query("perc > 0")
        labels = df_model_dist["model_type"].values
        values = df_model_dist["perc"].values

        fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.40)])
        fig.update(layout_showlegend=False)
        fig.update_layout(
            margin={"t": 0, "b": 0, "r": 20, "l": 20},
            width=200,
            height=150,
        )
        fig.update_traces(textposition="outside", textinfo="percent+label")
        st.plotly_chart(fig)

    acc = (1 - sr_err.err_mean) * 100.
    acc_naive = (1 - sr_err_naive.err_mean) * 100.

    with _cols[2]:
        st.markdown("#### Forecast Accuracy")
        st.markdown(
            f"<span style='font-size:36pt;font-weight:bold'>{acc:.0f}%</span><br/>"
            f"({acc - acc_naive:.0f}% increase vs. naive)", unsafe_allow_html=True)

    return


@st.cache
def make_downloads(df_pred, df_results):
    """
    """

    pred_fn = os.path.join(ST_DOWNLOADS_PATH, f"{state.uploaded_file.name}_fcast.csv")
    results_fn = os.path.join(ST_DOWNLOADS_PATH, f"{state.uploaded_file.name}_results.csv")

    state.df_pred.to_csv(pred_fn, index=False)
    state.df_results.to_csv(results_fn, index=False)

    return pred_fn, results_fn


def panel_downloads(state):
    """
    """

    pred_fn, results_fn = make_downloads(state.df_pred, state.df_results)
    pred_bn, results_bn = os.path.basename(pred_fn), os.path.basename(results_fn)

    st.markdown(
        f"Forecast: [{pred_bn}](downloads/{pred_bn})  \n"
        f"Results: [{results_bn}](downloads/{results_bn})\n",
        unsafe_allow_html=True
    )

    return


#
# Pages
#
def page_upload_file(state):
    """
    """
    with st.beta_container():
        st.header("Create Forecast")
        st.subheader("Step 1: Upload and validate a historic demand file")

        with st.form("form_select_file"): 
            state.uploaded_file = st.file_uploader("Select a .csv or .csv.gz file")

            _cols = st.beta_columns(1)

            with _cols[0]:
                state.freq_in = st.selectbox("Input Frequency", list(FREQ_MAP.keys())) 

            validate_button = st.form_submit_button("Validate")

        #
        # run validation process
        #
        if validate_button:
            #st.text(state.uploaded_file)
            if state.uploaded_file:
                with st.spinner("Validating file..."):
                    state.df_upload = load_uploaded_file(state.uploaded_file)
                    state.vldn_msgs, state.is_valid_file = validate(state.df_upload)
            else:
                st.error("Please select a file")

        # validation error messages
        if state.is_valid_file:
            # parse data into sfs format and impute missing dates according
            # to the input frequency
            state.df = load_data(state.df_upload, impute_freq=FREQ_MAP[state.freq_in])
        elif state.is_valid_file is None:
            pass
        else:
            err_bullets = \
                "\n\n".join("- " + s for s in state.vldn_msgs["errors"])
            st.error(f"Validation failed\n\n{err_bullets}")
            st.stop()

        #
        # run data health check
        #
        if state.is_valid_file and state.df is not None:
            panel_health_check(state)
            panel_launch_forecast(state)

        if state.df_pred is None and state.df_report is None:
            pass
        else:
            st.info('Forecast completed!, select "View Report" from the sidebar to view the forecast results')


    return


def page_create_forecast(state):
    """
    """

    freq_options = ["Daily", "Weekly", "Monthly"]

    with st.beta_container():
        st.subheader("Step 2: Select forecast parameters")

        # Display input file info.
        if state.uploaded_file:
            file_col1, file_col2, file_col3 = st.beta_columns([1,1,1])

            with file_col1:
                st.markdown(f"**{state.uploaded_file.name}**")

            with file_col2:
                st.markdown(f"Size: ~{state.uploaded_file.size / 1000**2:.1f} MB")
        else:
            state.uploaded_file = st.file_uploader("Select File")

            if state.uploaded_file:
                df, msgs, is_valid_file = validate(state.uploaded_file)
                state.is_valid_file = is_valid_file
                state.df = df

        col1, col2, col3 = st.beta_columns(3)

        with col1:
            state.in_freq = \
                st.selectbox("Input Frequency",
                    freq_options,
                    freq_options.index(state.in_freq) if state.in_freq else 0
                )

        with col2:
            state.horiz = \
                st.number_input("Forecast Horizon Length",
                    value=(state.horiz or 1), min_value=1)

        with col3:
            state.out_freq = \
                st.selectbox("Forecast Frequency",
                    freq_options,
                    freq_options.index(state.out_freq) if state.out_freq else 0)

        launch_button = st.button(label="Launch")

        if launch_button and state.is_valid_file:
            launch_sfs_forecast(state)

    return


def page_view_report(state):
    """
    """

    def make_mask(df, channel, family, item_id):
        mask = np.ones(len(df)).astype(bool)

        # only mask when all three keys are non-empty
        if channel == "" or family == "" or item_id == "":
            return ~mask

        mask &= df["channel"] == channel
        mask &= df["family"] == family
        mask &= df["item_id"] == item_id

        return mask

    def _plot_demand_classification(df_demand_cln):
        df_plot = pd.DataFrame({"category": ["short", "medium", "continuous"]})
        df_plot = df_plot.merge(
            df_demand_cln["category"].value_counts(normalize=True) \
                                   .reset_index() \
                                   .rename({"index": "category",
                                            "category": "frac"}, axis=1),
            on="category", how="left"
        )
        df_plot["frac"] *= 100

        fig = go.Figure(
            go.Bar(
                x=df_plot["category"], y=df_plot["frac"],
            )
        )

        fig.update_layout(
            margin={"t": 20, "b": 0, "r": 20, "l": 20},
            width=250,
            height=260,
        )

        st.plotly_chart(fig)

        return

    st.subheader("Report")

    if state.df_results is None or state.df_pred is None:
        st.text("Results not ready")
        st.stop()

    with st.beta_container():
        df_pred = state.df_pred

        if df_pred is None:
            st.markdown("Results not yet ready")
            return

        df_hist = state.df_pred.query("type == 'actual'")
        df_results = state.df_results \
                          .assign(_index="") \
                          .set_index("_index")
        
        channel_vals = [""] + sorted(df_results["channel"].unique())
        family_vals = [""] + sorted(df_results["family"].unique())
        item_id_vals = [""] + sorted(df_results["item_id"].unique())

        num_series = df_hist[["channel", "family", "item_id"]] \
                        .drop_duplicates() \
                        .shape[0]

        df_model_dist, sr_err, sr_err_naive, acc_increase = \
            make_perf_summary(df_results)

        cols = st.beta_columns(4)

        with cols[0]:
            n_top = 10
            st.subheader(f"Top {n_top}")
            st.markdown("#### By Demand")

            df_top = df_hist.groupby(GROUP_COLS, as_index=False) \
                            .agg({"demand": sum}) \
                            .sort_values(by="demand", ascending=False) \
                            .head(n_top) \
                            .reset_index(drop=True)

            #df_top["demand"] = df_top["demand"].apply(lambda x: f"{x:,.0f}")
            df_top = df_top.assign(_index=np.arange(n_top)+1).set_index("_index")

            #st.text(df_top.to_markdown(index=False, tablefmt="simple", floatfmt=",.0f"), headers=[])
            st.text(tabulate(df_top, floatfmt=",.0f", showindex="never",
                tablefmt="plain", headers="keys"))

        with cols[1]:
            st.subheader("Summary")
            st.markdown("#### Historical")
            st.text(f"No. Series: {num_series}\n"
                    f"Frequency:  {state.freq_in}\n"
                    f"Channels:   {len(channel_vals)-1}\n"
                    f"Families:   {len(family_vals)-1}\n"
                    f"Items:      {len(item_id_vals)-1}\n"
            )

            st.markdown("#### Forecast")
            st.text(f"Horizon:    {state.horiz}\n"
                    f"Frequency:  {state.freq_out}")

        with cols[2]:
            st.subheader("Demand Classification")
            _plot_demand_classification(state.df_demand_cln)

        with cols[3]:
            acc = (1 - sr_err.err_mean) * 100.
            acc_naive = (1 - sr_err_naive.err_mean) * 100.

            st.subheader("Performance")
            st.markdown("#### Forecast Accuracy")
            st.markdown(f"## {acc:.0f}%")
            st.markdown(f"(_{acc - acc_naive:.0f}% increase vs. naive_)")
            st.markdown("#### Best performing models")

            df_model_dist = df_model_dist.query("perc > 0")
            labels = df_model_dist["model_type"].values
            values = df_model_dist["perc"].values

            print(df_model_dist)

            fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.5)])
            fig.update(layout_showlegend=False)
            fig.update_layout(
                margin={"t": 20, "b": 0, "r": 20, "l": 20},
                width=200,
                height=200,
            )
            fig.update_traces(textposition="outside", textinfo="percent+label")
            st.plotly_chart(fig)


        st.subheader("Visualization")
        col1, col2 = st.beta_columns([1,4])

        with col1:
            st.markdown("#### Filter")

            # get default choices
            channel_index = channel_vals.index(df_top["channel"].iloc[0])
            family_index = family_vals.index(df_top["family"].iloc[0])
            item_id_index = item_id_vals.index(df_top["item_id"].iloc[0])

            channel_choice = st.selectbox("Channel", channel_vals, index=channel_index)
            family_choice = st.selectbox("Family", family_vals, index=family_index)
            item_id_choice = st.selectbox("Item ID", item_id_vals, index=item_id_index)

        with col2:
            results_mask = \
                make_mask(df_results, channel_choice, family_choice, item_id_choice)
            pred_mask = \
                make_mask(df_pred, channel_choice, family_choice, item_id_choice)

            df_plot = df_pred[pred_mask]

            st.markdown("#### Chart")

            if len(df_plot) > 0:

                # display the line chart
                #fig = px.line(df_plot, x="timestamp", y="demand", color="type")

                y = df_plot.query("type == 'actual'")["demand"]
                y_ts = df_plot.query("type == 'actual'")["timestamp"]

                yp = df_plot.query("type == 'fcast'")["demand"]
                yp_ts = df_plot.query("type == 'fcast'")["timestamp"]

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=y_ts, y=y, mode='lines+markers', name="actual",
                    marker=dict(size=4)
                ))
                fig.add_trace(go.Scatter(
                    x=yp_ts, y=yp, mode='lines+markers', name="forecast", line_dash="dot",
                    marker=dict(size=4)
                ))
        #       fig.update_layout(
        #           xaxis={
        #               "showgrid": True,
        #               "gridcolor": "lightgrey",
        #           },
        #           yaxis={
        #               "showgrid": True,
        #               "gridcolor": "lightgrey",
        #           }
        #       )
                fig.update_layout(
                        margin={"t": 30, "b": 0, "r": 0, "l": 40},
                    height=290,
                    legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor":"center", "x": 0.5}
                )
                st.plotly_chart(fig, use_container_width=True)

#       report_f = state.df_results.to_csv(index=False)
#       report_b64 = base64.b64encode(report_f.encode()).decode()
#       report_fn = f"{state.uploaded_file.name}_report.csv"

#       href_html = f"""
#       - <a href='data:file/csv;base64,{forecast_b64}' download='{forecast_fn}'>{forecast_fn}</a>
#       - <a href='data:file/csv;base64,{report_b64}' download='{report_fn}'>{report_fn}</a>
#       """

#       st.subheader("Downloads")
#       dl_button = st.button("Download Forecast")

#       if dl_button:
#           f = state.df_pred.to_csv(index=False)
#           forecast_b64 = base64.b64encode(f.encode()).decode()
#           forecast_fn = f"{state.uploaded_file.name}_forecast.csv"

#           report_f = state.df_results.to_csv(index=False)
#           report_b64 = base64.b64encode(report_f.encode()).decode()
#           report_fn = f"{state.uploaded_file.name}_report.csv"

#           href_html = f"""
#           - <a href="javascript:void(0)" onclick="location.href='data:file/csv;base64,{report_b64}'" download='{report_fn}'>{report_fn}</a>
#           """

#           st.markdown(href_html, unsafe_allow_html=True)

#       - <a href='data:file/csv;base64,{report_b64}' download='{report_fn}'>{report_fn}</a>
#       """



#       with col3:
#           st.markdown("#### Summary")

#       st.subheader("DEBUGGING OUTPUT")
#       st.text("df_results[mask]")
#       st.dataframe(df_pred[pred_mask])

#        if len(df) > 0:
#            #
#            # Display the chart
#            #
#            fig = px.line(df, x="timestamp", y="demand", color="type")
#
#    #       #fig.add_vline(x="2016-01-01", line_width=1, line_dash="dot")
#    #       #fig.add_vline(x="2017-01-01", line_width=1, line_dash="dot")
#
#            fig.update_layout(
#                xaxis={
#                    "showgrid": True,
#                    "gridcolor": "lightgrey",
#                },
#                yaxis={
#                    "showgrid": True,
#                    "gridcolor": "lightgrey",
#                }
#            )
#
#            st.plotly_chart(fig, use_container_width=True)
#

    return


def make_dataframes(state, wait_for):
    """
    """

    # aggregate the forecasts
    raw_results = [f.result() for f in futures.as_completed(wait_for)]

    pred_lst = []
    results_lst = []

    for df_pred, df_results in raw_results:
        pred_lst.append(df_pred)
        results_lst.append(df_results)

    # results dataframe
    state.df_results = pd.concat(results_lst) \
                         .reset_index(drop=True)

    # predictions dataframe
    state.df_pred = pd.concat(pred_lst)
    state.df_pred.index.name = "timestamp"
    state.df_pred.reset_index(inplace=True)

    # analysis dataframes
    state.df_demand_cln = \
        make_demand_classification(state.df, FREQ_MAP[state.freq_out])
    state.perf_summary = make_perf_summary(state.df_results)

    state.df_hist = state.df_pred.query("type == 'actual'")

    n_top = 10
    df_top = state.df_hist \
                  .groupby(GROUP_COLS, as_index=False) \
                  .agg({"demand": sum}) \
                  .sort_values(by="demand", ascending=False) \
                  .head(n_top) \
                  .reset_index(drop=True)

    df_top = df_top.assign(_index=np.arange(n_top)+1).set_index("_index")

    state.df_top = df_top

    return state


if __name__ == "__main__":
    #st.set_page_config(layout="wide")
    state = get_state()

    pages = {
        "Create Forecast": page_upload_file,
        "View Report": page_view_report
    }

    st.sidebar.title("Amazon Simple Forecast Solution")
    st.sidebar.markdown(textwrap.dedent("""
    """))
    st.title("Create Forecasts")
    st.markdown("")

    with st.beta_expander("0 – Prepare Data", expanded=True):
        panel_prepare_data(state)

    with st.beta_expander("1 – Load & Validate Data", expanded=True):
        if state.uploaded_file is None:
            uploaded_file = st.file_uploader("File")

            state.freq_in = st.selectbox("Frequency", list(FREQ_MAP.keys()))
            validate_button = st.button("Validate")

            if validate_button:
                state.uploaded_file = uploaded_file

                if state.uploaded_file:
                    with st.spinner("Validating file..."):
                        state.df_upload = load_uploaded_file(state.uploaded_file)
                        state.vldn_msgs, state.is_valid_file = validate(state.df_upload)
                else:
                    st.error("Please select a file")

            if state.is_valid_file and state.uploaded_file:
                # parse data into sfs format and impute missing dates according
                # to the input frequency
                state.df = load_data(state.df_upload, impute_freq=FREQ_MAP[state.freq_in])
                state.uploaded_file_name = state.uploaded_file.name

                st.info(f"Validation succeeded")
            elif state.is_valid_file is None:
                pass
            else:
                err_bullets = \
                    "\n\n".join("- " + s for s in state.vldn_msgs["errors"])
                st.error(f"Validation failed\n\n{err_bullets}")

        else:
            st.text(f"File:\t\t{state.uploaded_file.name}\n"
                    f"Size:\t\t~{state.uploaded_file.size/1e6:.1f} MB\n"
                    f"Frequency:\t{state.freq_in}")

    if state.df is not None and state.df_health is None:
        with st.spinner("Running data health check..."):
            state.df_health = \
                make_health_summary(state.df, FREQ_MAP[state.freq_in])

    #
    # Display validation health check status
    #
    if state.df_health is None:
        pass
    else:
        with st.beta_expander("2 – Data Health Check", expanded=True):
            panel_health_check(state)

    if state.is_valid_file and state.df is not None:
        with st.beta_expander("3 – Configure & Launch Forecast"):
            with st.beta_container():
                _cols = st.beta_columns(3)

                with _cols[0]:
                    state.horiz = st.number_input("Horizon Length", value=1, min_value=1)

                with _cols[1]:
                    state.freq_out = \
                        st.selectbox("Forecast Frequency",
                            list(FREQ_MAP.keys()),
                            list(FREQ_MAP.keys()).index(state.freq_out) if state.freq_out else 0)

                with _cols[2]:
                    state.backend = \
                        st.selectbox("Compute Backend", ["AWS Lambda", "Local"], 0)

                launch_button = st.button("Launch")

    #
    # Launch a forecast job
    #
    if state.is_valid_file and isinstance(state.df, pd.DataFrame) and launch_button and state.backend:
        if state.backend == 'Local':
            wait_for = \
                run_pipeline(state.df, FREQ_MAP[state.freq_in],
                    FREQ_MAP[state.freq_out], obj_metric="smape_mean",
                    cv_stride=2, backend="futures", horiz=state.horiz)
        elif state.backend == 'AWS Lambda':
            # Resample data
            with st.spinner(f"Resampling to {state.freq_out} frequency"):
                state.df2 = resample(state.df, FREQ_MAP[state.freq_out])

            wait_for = \
                run_lambdamap(state.df2, state.horiz, FREQ_MAP[state.freq_out])
        else:
            raise NotImplementedError

        display_progress(wait_for, "Generating forecast")

        with st.spinner("Processing Results"):
            make_dataframes(state, wait_for)

    #
    # Display Visualization Panel
    #
    if state.df_pred is None and state.df_results is None:
        pass
    else:
        with st.beta_expander("4 – Forecast Summary", expanded=True):
            panel_forecast_summary(state)

        with st.beta_expander("5 – Visualize Forecast", expanded=True):
            panel_visualization(state)

        with st.beta_expander("6 – Downloads", expanded=True):
            panel_downloads(state)

    state.sync()