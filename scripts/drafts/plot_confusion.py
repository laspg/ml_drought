from sklearn.metrics import confusion_matrix
from sklearn.utils.multiclass import unique_labels
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from typing import Optional


def vdi_confusion_matrix(
    vdi_true: xr.DataArray,
    vdi_pred: xr.DataArray,
    normalize: bool = False,
    title: Optional[str] = None,
    **kwargs,
) -> plt.Axes:
    # 1. convert xarray -> numpy format
    true_np = vdi_true.stack(pixel=["lat", "lon"]).values.flatten().clip(min=1, max=5)
    pred_np = vdi_pred.stack(pixel=["lat", "lon"]).values.flatten().clip(min=1, max=5)

    # plot confusion matrix
    ax = plot_confusion_matrix(
        y_true=true_np, y_pred=pred_np, normalize=normalize, title=title, **kwargs
    )
    return ax


def plot_confusion_matrix(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    #  classes: Optional[] = None,
    normalize: bool = False,
    title: Optional[str] = None,
    cmap=plt.cm.Blues,
    **imshow_kwargs,
) -> plt.Axes:
    """
    This function prints and plots the confusion matrix.
    Normalization can be applied by setting `normalize=True`.

    Alternative for imshow: ax.pcolor
    """
    if not title:
        if normalize:
            title = "Normalized confusion matrix"
        else:
            title = "Confusion matrix, without normalization"

    # Compute confusion matrix
    cm = confusion_matrix(y_true, y_pred)
    # Only use the labels that appear in the data
    #     classes = classes[unique_labels(y_true, y_pred)]
    if normalize:
        cm = cm.astype("float") / cm.sum(axis=1)[:, np.newaxis]
        print("Normalized confusion matrix")
    else:
        print("Confusion matrix, without normalization")

    print(cm)

    fig, ax = plt.subplots(figsize=(12, 8))
    im = ax.imshow(cm, interpolation="nearest", cmap=cmap, **imshow_kwargs)
    ax.figure.colorbar(im, ax=ax)
    # We want to show all ticks...
    ax.set(
        xticks=np.arange(cm.shape[1]),
        yticks=np.arange(cm.shape[0]),
        # ... and label them with the respective list entries
        #  xticklabels=classes, yticklabels=classes,
        title=title,
        ylabel="True label",
        xlabel="Predicted label",
    )

    # Rotate the tick labels and set their alignment.
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", rotation_mode="anchor")

    # Loop over data dimensions and create text annotations.
    fmt = ".2f" if normalize else "d"
    thresh = cm.max() / 2.0
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(
                j,
                i,
                format(cm[i, j], fmt),
                ha="center",
                va="center",
                color="white" if cm[i, j] > thresh else "black",
            )

    fig.tight_layout()
    return ax