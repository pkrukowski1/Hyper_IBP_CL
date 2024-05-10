import os
import torch
import pandas as pd
import numpy as np
import torch.nn.functional as F
from datetime import datetime

from evaluation import (
    prepare_and_load_weights_for_models,
)
from FeCAM import (
    extract_test_set_from_single_task,
    translate_output_CIFAR_classes,
    get_target_network_representation,
    translate_output_MNIST_classes,
)


def get_task_and_class_prediction_based_on_logits(
    inferenced_logits_of_all_tasks, setup, dataset, vanilla_entropy = False
):
    """
    Get task prediction for consecutive samples based on interval 
    entropy values of the output classification layer of the target network.

    Arguments:
    ----------
       *inferenced_logits_of_all_tasks*: shape: (number of tasks,
                            number of samples, number of output heads)
       *setup*: (int) defines how many tasks were performed in this
                experiment (in total)
       *dataset*: (str) name of the dataset for proper class translation

    Returns:
    --------
       *predicted_tasks*: torch.Tensor with the prediction of tasks for
                          consecutive samples
       *predicted_classes*: torch.Tensor with the prediction of classes for
                            consecutive samples.
       Positions of samples in the two above Tensors are the same.
    """
    predicted_classes, predicted_tasks = [], []
    number_of_samples = inferenced_logits_of_all_tasks.shape[1]

    for no_of_sample in range(number_of_samples):
        task_entropies = torch.zeros((inferenced_logits_of_all_tasks.shape[0]))

        all_task_single_output_sample = inferenced_logits_of_all_tasks[
            :, no_of_sample, :, :
        ]

        # Calculate entropy based on results from all tasks
        for no_of_inferred_task in range(task_entropies.shape[0]):

            softmaxed_inferred_task = F.softmax(
                all_task_single_output_sample[no_of_inferred_task, 1, :], dim=-1
            )
            
            if not vanilla_entropy:
                lower_logits = all_task_single_output_sample[no_of_inferred_task, 0, :]
                upper_logits = all_task_single_output_sample[no_of_inferred_task, 2, :]

                factor = 1 / (0.0001 + (upper_logits - lower_logits).abs())
            else:
                factor = 1.0
            
            task_entropies[no_of_inferred_task] = -1 * torch.sum(factor * \
                softmaxed_inferred_task * torch.log(softmaxed_inferred_task), dim=-1
            )
        
        selected_task_id = torch.argmin(task_entropies)
        predicted_tasks.append(selected_task_id.item())

        # We evaluate performance of classification task on middle
        # logits only 
        target_output = all_task_single_output_sample[selected_task_id.item(), 1, :]

        output_relative_class = target_output.argmax().item()

        if dataset == "CIFAR100_FeCAM_setup":
            output_absolute_class = translate_output_CIFAR_classes(
                [output_relative_class], setup, selected_task_id.item()
            )
        elif dataset in ["PermutedMNIST", "SplitMNIST"]:
            mode = "permuted" if dataset == "PermutedMNIST" else "split"
            output_absolute_class = translate_output_MNIST_classes(
                [output_relative_class], selected_task_id.item(), mode=mode
            )
        else:
            raise ValueError("Wrong name of the dataset!")
        predicted_classes.append(output_absolute_class)
    predicted_tasks = torch.tensor(predicted_tasks, dtype=torch.int32)
    predicted_classes = torch.tensor(predicted_classes, dtype=torch.int32)
    return predicted_tasks, predicted_classes


def calculate_entropy_and_predict_classes_separately(experiment_models):
    """
    Select the target task automatically and calculate accuracy for
    consecutive samples

    Arguments:
    ----------
    *experiment_models*: A dictionary with the following keys:
       *hypernetwork*: an instance of HMLP class
       *hypernetwork_weights*: loaded weights for the hypernetwork
       *target_network*: an instance of MLP or ResNet class
       *target_network_weights*: loaded weights for the target network
       *hyperparameters*: a dictionary with experiment's hyperparameters
       *dataset_CL_tasks*: list of objects containing consecutive tasks

    Returns Pandas Dataframe with results for the selected model.
    """
    hypernetwork = experiment_models["hypernetwork"]
    hypernetwork_weights = experiment_models["hypernetwork_weights"]
    target_network = experiment_models["target_network"]
    hyperparameters = experiment_models["hyperparameters"]
    dataset_CL_tasks = experiment_models["list_of_CL_tasks"]
    dataset_name = experiment_models["hyperparameters"]["dataset"]
    target_network_type = hyperparameters["target_network"]
    saving_folder = hyperparameters["saving_folder"]
    alpha = hyperparameters["alpha"]
    full_interval = hyperparameters["full_interval"]
    vanilla_entropy = experiment_models["vanilla_entropy"]

    hypernetwork.eval()
    target_network.eval()

    results = []
    for task in range(hyperparameters["number_of_tasks"]):

        X_test, y_test, gt_tasks = extract_test_set_from_single_task(
            dataset_CL_tasks, task, dataset_name, hyperparameters["device"]
        )

        with torch.no_grad():
            logits_outputs_for_different_tasks = []
            for inferenced_task in range(hyperparameters["number_of_tasks"]):

                # Try to predict task for all samples from "task"
                logits = get_target_network_representation(
                    hypernetwork,
                    hypernetwork_weights,
                    target_network,
                    target_network_type,
                    X_test,
                    inferenced_task,
                    alpha,
                    full_interval
                )


                logits_outputs_for_different_tasks.append(logits)

            all_inferenced_tasks = torch.stack(
                logits_outputs_for_different_tasks
            )
            # Sizes of consecutive dimensions represent:
            # number of tasks x number of samples x 3 x number of output heads
        (
            predicted_tasks,
            predicted_classes,
        ) = get_task_and_class_prediction_based_on_logits(
            all_inferenced_tasks,
            hyperparameters["number_of_tasks"],
            dataset_name,
            vanilla_entropy=vanilla_entropy
        )
        predicted_classes = predicted_classes.flatten().numpy()
        task_prediction_accuracy = (
            torch.sum(predicted_tasks == task).float()
            * 100.0
            / predicted_tasks.shape[0]
        ).item()
        print(f"task prediction accuracy: {task_prediction_accuracy}")
        sample_prediction_accuracy = (
            np.sum(predicted_classes == y_test) * 100.0 / y_test.shape[0]
        ).item()
        print(f"sample prediction accuracy: {sample_prediction_accuracy}")
        results.append(
            [task, task_prediction_accuracy, sample_prediction_accuracy]
        )
    results = pd.DataFrame(
        results, columns=["task", "task_prediction_acc", "class_prediction_acc"]
    )
    results.to_csv(
        f"{saving_folder}entropy_statistics_{number_of_model}.csv", sep=";"
    )
    return results


if __name__ == "__main__":
    # The results are varying depending on the batch sizes due to the fact
    # that batch normalization is turned on in ResNet. We selected 2000 as
    # the test set size to ensure that it is derived to the network
    # in one piece.
    
    alphas = [0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5]
    batch_inference_size = 2000
    vanilla_entropy = True

    # Options for *dataset*:
    # 'PermutedMNIST', 'SplitMNIST', 'CIFAR100_FeCAM_setup', 'SubsetImageNet'
    dataset = "PermutedMNIST"
    path_to_datasets = "./Data/"

    for alpha in alphas:

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") # Generate timestamp
        path_to_stored_networks = f"./SavedModels/{dataset}/known_task_id/"
        path_to_save = f"./Results/{dataset}/{timestamp}/"
        os.makedirs(path_to_save, exist_ok=True)

        results_summary = []
        numbers_of_models = [i for i in range(5)]
        seeds = [i + 1 for i in range(5)]

        dict_to_save = {}


        for number_of_model, seed in zip(numbers_of_models, seeds):
            print(f"Calculations for model no: {number_of_model}")
            experiment_models = prepare_and_load_weights_for_models(
                path_to_stored_networks,
                path_to_datasets,
                number_of_model,
                dataset,
                seed=seed,
            )

            experiment_models["hyperparameters"]["saving_folder"] = path_to_save
            experiment_models["hyperparameters"]["alpha"] = alpha
            experiment_models["vanilla_entropy"] = vanilla_entropy

            results = calculate_entropy_and_predict_classes_separately(
                experiment_models
            )
            results_summary.append(results)
            
        data_statistics = []
        for summary in results_summary:
            data_statistics.append(
                [
                    list(summary["task_prediction_acc"].values),
                    list(summary["class_prediction_acc"].values),
                    np.mean(summary["task_prediction_acc"].values),
                    np.std(summary["task_prediction_acc"].values),
                    np.mean(summary["class_prediction_acc"].values),
                    np.std(summary["class_prediction_acc"].values),
                ]
            )
        column_names = [
            "task_prediction_accuracy",
            "class_prediction_accuracy",
            "mean_task_prediction_accuracy",
            "std_dev_task_prediction_accuracy",
            "mean_class_prediction_accuracy",
            "std_dev_class_prediction_accuracy",
        ]
        table_to_save = data_statistics
        dataframe = pd.DataFrame(table_to_save, columns=column_names)
        dataframe.to_csv(
            f"{path_to_save}entropy_mean_results",
            sep=";",
        )

        dict_to_save["alpha"] = [experiment_models["hyperparameters"]["alpha"]]
        dict_to_save["final_mean"] = np.mean(dataframe["mean_class_prediction_accuracy"])
        dict_to_save["final_stdev"] = np.std(dataframe["mean_class_prediction_accuracy"])
        dataframe = pd.DataFrame.from_dict(dict_to_save)
        dataframe.to_csv(
            f"{path_to_save}hyperparameters",
            sep=";",
        )


