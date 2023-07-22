from os import makedirs
import importlib
import contextlib
import json
from shutil import copy
import torch


from src.utils_data import (
    load_PeMS04_flow_data,
    preprocess_PeMS_data,
    local_dataset,
    plot_prediction,
)
from src.utils_training import train_model, testmodel
from src.utils_fed import fed_training_plan
from src.metrics import calculate_metrics
import src.config
import sys

import contextlib


seed = 42
torch.manual_seed(seed)


# Get the path to the configuration file from the command-line arguments
if len(sys.argv) != 2:
    print("Usage: python3 experiment.py CONFIG_FILE_PATH")
    sys.exit(1)
config_file_path = sys.argv[1]

params = src.config.Params(config_file_path)

PATH_EXPERIMENTS = f"experiments/{params.save_model_path}"

makedirs(PATH_EXPERIMENTS, exist_ok=True)
copy(
    sys.argv[1],
    f"{PATH_EXPERIMENTS}config.json",
)

with open(f"{PATH_EXPERIMENTS}train.txt", "w") as f:
    with contextlib.redirect_stdout(src.config.Tee(f, sys.stdout)):
        module_name = "src.models"
        class_name = params.model
        module = importlib.import_module(module_name)
        model = getattr(module, class_name)

        input_size = 1
        hidden_size = 32
        num_layers = 6
        output_size = 1

        # Load traffic flow dataframe and graph dataframe from PEMS
        df_PeMS, distance = load_PeMS04_flow_data()
        df_PeMS = df_PeMS[: int(len(df_PeMS) * params.time_serie_percentage_length)]
        print(len(df_PeMS))
        df_PeMS, adjmat, meanstd_dict = preprocess_PeMS_data(
            df_PeMS,
            distance,
            params.init_node,
            params.n_neighbours,
            params.smooth,
            params.center_and_reduce,
            params.normalize,
            params.sort_by_mean,
        )
        if params.nodes_to_filter == []:
            params.nodes_to_filter = list(df_PeMS.columns[: params.number_of_nodes])
            with open(f"{PATH_EXPERIMENTS}config.json", "r") as file:
                data = json.load(file)
                data["nodes_to_filter"] = params.nodes_to_filter
                with open(f"{PATH_EXPERIMENTS}config.json", "w") as file:
                    json.dump(data, file, indent=4, separators=(",", ": "))

        print(params.nodes_to_filter)
        datadict = local_dataset(
            df=df_PeMS,
            nodes=params.nodes_to_filter,
            window_size=params.window_size,
            stride=params.stride,
            prediction_horizon=params.prediction_horizon,
            batch_size=params.batch_size,
        )

        if params.num_epochs_local_no_federation:
            # Local Training
            train_losses = {}
            val_losses = {}

            for node in range(params.number_of_nodes):
                local_model = model(input_size, hidden_size, output_size, num_layers)

                data_dict = datadict[node]
                local_model, train_losses[node], val_losses[node] = train_model(
                    local_model,
                    data_dict["train"],
                    data_dict["val"],
                    model_path=f"{PATH_EXPERIMENTS}local{node}.pth",
                    num_epochs=params.num_epochs_local_no_federation,
                    remove=False,
                    learning_rate=params.learning_rate,
                )

        # # Federated Learning Experiment
        if params.num_epochs_local_federation:
            main_model = model(input_size, hidden_size, output_size, num_layers)

            fed_training_plan(
                main_model,
                datadict,
                params.communication_rounds,
                params.num_epochs_local_federation,
                model_path=f"{PATH_EXPERIMENTS}",
            )

        if params.epoch_local_retrain_after_federation:
            # Local Training
            train_losses = {}
            val_losses = {}

            for node in range(params.number_of_nodes):
                print(
                    f"Retraining the federated model locally on node {node} for {params.epoch_local_retrain_after_federation} epochs"
                )
                new_local_model = model(
                    input_size, hidden_size, output_size, num_layers
                )
                model_path = f"{PATH_EXPERIMENTS}bestmodel_node{node}.pth"
                local_model = model(input_size, hidden_size, output_size, num_layers)
                local_model.load_state_dict(torch.load(model_path))
                torch.save(
                    local_model.state_dict(),
                    f"{PATH_EXPERIMENTS}oldmodel_node{node}.pth",
                )

                data_dict = datadict[node]
                local_model, train_losses[node], val_losses[node] = train_model(
                    new_local_model,
                    data_dict["train"],
                    data_dict["val"],
                    model_path=f"{PATH_EXPERIMENTS}bestmodel_node{node}.pth",
                    num_epochs=params.epoch_local_retrain_after_federation,
                    remove=False,
                    learning_rate=params.learning_rate,
                )
